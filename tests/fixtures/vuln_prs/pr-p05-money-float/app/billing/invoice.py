def calc_total(items):
    total = 0.0
    for it in items:
        total += it.price * it.qty
    return round(total, 2)
