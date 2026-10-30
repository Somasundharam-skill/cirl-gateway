from app.masking import mask_email, mask_last4, mask_mobile_cirl, mask_payload, mask_phone, redact_values


def test_mask_formats():
    assert mask_last4("1921008101111") == "XXXXXXXXX1111"
    assert mask_last4("36521478") == "XXXX1478"
    assert mask_last4("P1") == "XXXXP1"
    assert mask_phone("+917373345820") == "+91XXXXXX5820"
    assert mask_mobile_cirl("7373345820") == "73XXXXXX20"
    assert mask_email("ramesh.kumar@example.com") == "ra*****@example.com"


def test_mask_payload_keys_and_safety_net():
    masked = mask_payload(
        {
            "otp_no": "123456",
            "eia_no": "1921008101111",
            "policy_no": "36521478",
            "phone": "+917373345820",
            "note": "eIA 1921008101111, mail ramesh.kumar@example.com, UID 123412341234, PAN ABCPE1234E",
        }
    )
    assert masked["otp_no"] == "******"
    assert masked["eia_no"] == "XXXXXXXXX1111"
    assert masked["policy_no"] == "XXXX1478"
    assert masked["phone"] == "+91XXXXXX5820"
    assert masked["note"] == "eIA XXXXXXXXX1111, mail ra*****@example.com, UID XXXXXXXX1234, PAN ABCXXXX34E"


def test_already_masked_values_are_left_alone():
    text = "mobile 73XXXXXX20 and email ra*****@example.com"
    assert mask_payload({"m": text}) == {"m": text}


def test_redact_values():
    payload = {"a": "Policy No: 36521478\nPolicy No: 365214780", "b": ["36521478"]}
    out = redact_values(payload, {"36521478": "XXXX1478", "365214780": "XXXXX4780"})
    assert out == {"a": "Policy No: XXXX1478\nPolicy No: XXXXX4780", "b": ["XXXX1478"]}
