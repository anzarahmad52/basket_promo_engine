import frappe
from frappe.utils import flt


PROMO_TAG = "BASKET_PROMO"


def apply_promotions(doc, method=None):
    # Draft only
    if getattr(doc, "docstatus", 0) != 0:
        return

    # Skip return docs
    if getattr(doc, "is_return", 0):
        return

    if not getattr(doc, "items", None):
        return

    rule = _get_matching_rule(doc)
    if not rule:
        _remove_existing_promo_rows(doc)
        return

    eligible_items = set(rule.get("eligible_items") or [])
    slabs = rule.get("slabs") or []

    basket_qty, item_qty_map, best_row_by_item = _compute_basket_qty(doc, eligible_items)

    free_qty = _get_free_qty_for_slab(basket_qty, slabs)
    if free_qty <= 0:
        _remove_existing_promo_rows(doc)
        return

    free_item_code = _select_free_item(rule, item_qty_map)
    if not free_item_code:
        _remove_existing_promo_rows(doc)
        return

    # clean old promo rows to avoid duplicates each save
    _remove_existing_promo_rows(doc)

    source_row = best_row_by_item.get(free_item_code) or _find_any_eligible_row(doc, eligible_items)
    if not source_row:
        return

    _add_free_row(doc, source_row, free_item_code, free_qty)

    _recalc(doc)


# ---------------------------
# RULE LOOKUP (parent + child customer groups supported)
# ---------------------------

def _get_matching_rule(doc) -> dict | None:
    """
    Picks the best matching Basket Promo Rule based on:
    - enabled
    - customer_group: rule group can be the same as doc group OR a PARENT of doc group (applies to all children)
    - company (optional)
    - priority (higher first)
    """
    customer_group = (getattr(doc, "customer_group", None) or "").strip()
    company = (getattr(doc, "company", None) or "").strip()

    if not customer_group:
        return None

    # Get lft/rgt for selected customer group (nested set)
    cg = frappe.db.get_value("Customer Group", customer_group, ["lft", "rgt"], as_dict=True)
    if not cg:
        return None

    doc_lft = flt(cg.lft)
    doc_rgt = flt(cg.rgt)

    # Fetch all enabled rules and pick best match in python
    # (we must check ancestor/child relationship using lft/rgt)
    rules = frappe.get_all(
        "Basket Promo Rule",
        filters={"enabled": 1},
        fields=["name", "company", "priority", "free_item_policy", "fixed_free_item_code", "customer_group"],
        order_by="priority desc, modified desc",
    )

    if not rules:
        return None

    candidates = []
    for r in rules:
        rule_cg = (r.customer_group or "").strip()
        if not rule_cg:
            continue

        rule_lr = frappe.db.get_value("Customer Group", rule_cg, ["lft", "rgt"], as_dict=True)
        if not rule_lr:
            continue

        rule_lft = flt(rule_lr.lft)
        rule_rgt = flt(rule_lr.rgt)

        # Rule applies if rule group is same as doc group OR rule group is an ancestor of doc group
        applies_by_group = (rule_lft <= doc_lft <= doc_rgt <= rule_rgt)
        if not applies_by_group:
            continue

        # Company match rules:
        # - Prefer exact company match if rule has company
        # - Otherwise allow company blank
        company_ok = (not r.company) or (company and r.company == company)
        if not company_ok:
            continue

        candidates.append(r)

    if not candidates:
        return None

    # Prefer company-specific rule first (if any), otherwise generic
    chosen = None
    for r in candidates:
        if r.company and company and r.company == company:
            chosen = r
            break
    if not chosen:
        chosen = candidates[0]

    # Fetch child items + slabs
    eligible_items = frappe.get_all(
        "Basket Promo Rule Item",
        filters={"parent": chosen.name, "parenttype": "Basket Promo Rule"},
        pluck="item_code",
    )

    slab_rows = frappe.get_all(
        "Basket Promo Slab",
        filters={"parent": chosen.name, "parenttype": "Basket Promo Rule"},
        fields=["min_qty", "max_qty", "free_qty"],
        order_by="min_qty asc",
    )

    return {
        "name": chosen.name,
        "company": chosen.company,
        "priority": chosen.priority or 0,
        "free_item_policy": chosen.free_item_policy or "highest_qty",
        "fixed_free_item_code": chosen.fixed_free_item_code,
        "eligible_items": eligible_items,
        "slabs": slab_rows,
    }


# ---------------------------
# BASKET COMPUTE
# ---------------------------

def _compute_basket_qty(doc, eligible_items: set):
    basket_qty = 0.0
    item_qty_map = {}
    best_row_by_item = {}

    for row in doc.items:
        if _is_promo_row(row):
            continue

        item_code = (row.item_code or "").strip()
        if item_code not in eligible_items:
            continue

        qty = flt(getattr(row, "stock_qty", 0)) or flt(getattr(row, "qty", 0))
        if qty <= 0:
            continue

        basket_qty += qty
        item_qty_map[item_code] = item_qty_map.get(item_code, 0) + qty

        # choose a best row to copy warehouse/uom/conversion_factor/accounts/taxes from
        if item_code not in best_row_by_item or qty > flt(best_row_by_item[item_code].qty):
            best_row_by_item[item_code] = row

    return basket_qty, item_qty_map, best_row_by_item


def _get_free_qty_for_slab(basket_qty: float, slabs: list) -> float:
    for s in slabs:
        min_q = flt(s.get("min_qty"))
        max_q = flt(s.get("max_qty"))
        free_q = flt(s.get("free_qty"))

        # Support open-ended slab: max_qty blank/0 => infinity
        if max_q <= 0:
            if basket_qty >= min_q:
                return free_q

        if min_q <= basket_qty < max_q:
            return free_q

    return 0.0


def _select_free_item(rule: dict, item_qty_map: dict) -> str | None:
    policy = rule.get("free_item_policy") or "highest_qty"

    if policy == "fixed_item":
        return (rule.get("fixed_free_item_code") or "").strip() or None

    # default: highest purchased qty sku becomes free sku
    if not item_qty_map:
        return None

    return max(item_qty_map, key=item_qty_map.get)


# ---------------------------
# APPLY / CLEANUP
# ---------------------------

def _add_free_row(doc, source_row, item_code: str, qty: float):
    """
    Append free row and fill mandatory fields required by ERPNext + ZATCA apps.

    ZATCA app rule in your system:
    If any one item has Item Tax Template, all items must have Item Tax Template.
    So free row must copy item_tax_template (and tax_category if used).
    """

    delivery_date = getattr(source_row, "delivery_date", None) or getattr(doc, "delivery_date", None)

    row = doc.append("items", {
        "item_code": item_code,
        "qty": qty,
        "rate": 0,
        "price_list_rate": 0,
        "amount": 0,
        "warehouse": getattr(source_row, "warehouse", None),
        "uom": getattr(source_row, "uom", None),
        "conversion_factor": getattr(source_row, "conversion_factor", 1),
        "delivery_date": delivery_date,
        "description": f"FREE ITEM ({PROMO_TAG})",
    })

    # Mandatory UI field (to avoid "Item Name" missing popup)
    if not getattr(row, "item_name", None):
        row.item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code

    # Mandatory accounting fields (especially Sales Invoice)
    if hasattr(row, "income_account") and not getattr(row, "income_account", None):
        row.income_account = getattr(source_row, "income_account", None) or getattr(doc, "income_account", None)

    if hasattr(row, "cost_center") and not getattr(row, "cost_center", None):
        row.cost_center = getattr(source_row, "cost_center", None) or getattr(doc, "cost_center", None)

    # ZATCA consistency: copy item tax template / tax category from source row
    if hasattr(row, "item_tax_template"):
        row.item_tax_template = getattr(source_row, "item_tax_template", None)

    if hasattr(row, "tax_category"):
        row.tax_category = getattr(source_row, "tax_category", None)

    return row


def _remove_existing_promo_rows(doc):
    doc.items = [r for r in doc.items if not _is_promo_row(r)]


def _is_promo_row(row) -> bool:
    return (row.description or "").startswith("FREE ITEM") and PROMO_TAG in (row.description or "")


def _find_any_eligible_row(doc, eligible_items: set):
    for row in doc.items:
        if not _is_promo_row(row) and (row.item_code or "").strip() in eligible_items:
            return row
    return None


def _recalc(doc):
    # recalc totals after adding free row
    if hasattr(doc, "calculate_taxes_and_totals"):
        doc.calculate_taxes_and_totals()
