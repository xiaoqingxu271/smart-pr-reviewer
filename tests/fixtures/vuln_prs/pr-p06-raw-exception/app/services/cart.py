COUPONS = {}


def apply_coupon(code):
    if code not in COUPONS:
        raise RuntimeError("coupon not found")
    return COUPONS[code]
