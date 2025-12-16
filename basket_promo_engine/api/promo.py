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

def _get_matching_rule(doc) -> dict | None:
    """
    Picks the best matching Basket Promo Rule based on:
    - enabled
    - customer_group: match exact OR any ancestor group rule (parent applies to child)
    - company (optional)
    - priority (higher first)
    - specificity: child-group rule overrides parent-group rule
    """
    customer_group = (getattr(doc, "customer_group", None) or "").strip()
    company = (getattr(doc, "company", None) or "").strip()

    if not customer_group:
        return None

    cg_path = _get_customer_group_ancestors_including_self(customer_group)
    if not cg_path:
        return None

    rules = frappe.get_all(
        "Basket Promo Rule",
        filters={
            "enabled": 1,
            "customer_group": ["in", cg_path],
        },
        fields=["name", "company", "priority", "free_item_policy", "fixed_free_item_code", "customer_group"],
        order_by="priority desc, modified desc",
    )

    if not rules:
        return None
    company_matched = []
    company_blank = []

    for r in rules:
        if r.company and company and r.company == company:
            company_matched.append(r)
        elif not r.company:
            company_blank.append(r)

    candidates = company_matched + company_blank
    if not candidates:
        return None
    def specificity_index(rule_row):
        try:
            return cg_path.index(rule_row.customer_group)
        except Exception:
            return 999

    candidates.sort(key=lambda r: (-(r.priority or 0), specificity_index(r)))

    chosen = candidates[0]

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


def _get_customer_group_ancestors_including_self(customer_group: str) -> list[str]:
    """
    Returns [self, parent, ..., root] using nested set (lft/rgt).
    """
    row = frappe.db.get_value("Customer Group", customer_group, ["lft", "rgt"], as_dict=True)
    if not row:
        return [customer_group]

    ancestors = frappe.get_all(
        "Customer Group",
        filters={"lft": ["<=", row.lft], "rgt": [">=", row.rgt]},
        pluck="name",
        order_by="lft desc",  
    )
    return ancestors or [customer_group]
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
        if item_code not in best_row_by_item or qty > flt(best_row_by_item[item_code].qty):
            best_row_by_item[item_code] = row

    return basket_qty, item_qty_map, best_row_by_item


def _get_free_qty_for_slab(basket_qty: float, slabs: list) -> float:
    for s in slabs:
        min_q = flt(s.get("min_qty"))
        max_q = flt(s.get("max_qty"))
        free_q = flt(s.get("free_qty"))
        if min_q <= basket_qty < max_q:
            return free_q
    return 0.0


def _select_free_item(rule: dict, item_qty_map: dict) -> str | None:
    policy = rule.get("free_item_policy") or "highest_qty"

    if policy == "fixed_item":
        return (rule.get("fixed_free_item_code") or "").strip() or None

    if not item_qty_map:
        return None

    return max(item_qty_map, key=item_qty_map.get)
def _add_free_row(doc, source_row, item_code: str, qty: float):
    delivery_date = getattr(source_row, "delivery_date", None) or getattr(doc, "delivery_date", None)
    item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code

    doc.append("items", {
        "item_code": item_code,
        "item_name": item_name,
        "delivery_date": delivery_date,
        "qty": qty,
        "rate": 0,
        "price_list_rate": 0,
        "amount": 0,
        "warehouse": source_row.warehouse,
        "uom": source_row.uom,
        "conversion_factor": source_row.conversion_factor,
        "description": f"FREE ITEM ({PROMO_TAG})",
    })


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
    doc.calculate_taxes_and_totals()
