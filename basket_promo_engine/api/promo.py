import frappe
from frappe.utils import flt
from frappe.utils.nestedset import get_ancestors_of


PROMO_TAG = "BASKET_PROMO"


def apply_promotions(doc, method=None):
    if getattr(doc, "docstatus", 0) != 0:
        return
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
    - customer_group (supports parent rules for child groups)
    - company (optional)
    - priority (higher first)
    """
    customer_group = (getattr(doc, "customer_group", None) or "").strip()
    company = (getattr(doc, "company", None) or "").strip()

    if not customer_group:
        return None
    groups = [customer_group]
    try:
        groups += get_ancestors_of("Customer Group", customer_group) or []
    except Exception:
        pass

    rules = frappe.get_all(
        "Basket Promo Rule",
        filters={"enabled": 1, "customer_group": ["in", groups]},
        fields=["name", "company", "priority", "free_item_policy", "fixed_free_item_code", "customer_group"],
        order_by="priority desc, modified desc",
    )

    if not rules:
        return None
    chosen = None
    for r in rules:
        if r.company and company and r.company == company:
            chosen = r
            break
    if not chosen:
        for r in rules:
            if not r.company:
                chosen = r
                break

    if not chosen:
        return None

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
        "customer_group": chosen.customer_group,
        "free_item_policy": chosen.free_item_policy or "highest_qty",
        "fixed_free_item_code": chosen.fixed_free_item_code,
        "eligible_items": eligible_items,
        "slabs": slab_rows,
    }
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
        max_q = flt(s.get("max_qty") or 0)
        free_q = flt(s.get("free_qty"))
        if min_q <= basket_qty and (max_q <= 0 or basket_qty < max_q):
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
    """
    IMPORTANT:
    Make sure mandatory fields are set, especially for Sales Invoice:
    - income_account
    - cost_center
    and for Sales Order (often):
    - delivery_date
    - item_name
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
    if not getattr(row, "item_name", None):
        row.item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
    if hasattr(row, "income_account") and not getattr(row, "income_account", None):
        row.income_account = getattr(source_row, "income_account", None) or getattr(doc, "income_account", None)

    if hasattr(row, "cost_center") and not getattr(row, "cost_center", None):
        row.cost_center = getattr(source_row, "cost_center", None) or getattr(doc, "cost_center", None)
    company = (getattr(doc, "company", None) or "").strip()
    if company:
        if hasattr(row, "cost_center") and not getattr(row, "cost_center", None):
            row.cost_center = frappe.db.get_value("Company", company, "cost_center")
        if hasattr(row, "income_account") and not getattr(row, "income_account", None):
            row.income_account = frappe.db.get_value("Company", company, "default_income_account")


def _remove_existing_promo_rows(doc):
    for r in list(doc.items):
        if _is_promo_row(r):
            doc.remove(r)


def _is_promo_row(row) -> bool:
    return (row.description or "").startswith("FREE ITEM") and PROMO_TAG in (row.description or "")


def _find_any_eligible_row(doc, eligible_items: set):
    for row in doc.items:
        if not _is_promo_row(row) and (row.item_code or "").strip() in eligible_items:
            return row
    return None


def _recalc(doc):
    if hasattr(doc, "calculate_taxes_and_totals"):
        doc.calculate_taxes_and_totals()
