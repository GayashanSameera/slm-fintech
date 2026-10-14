"""Text grounding (grounding.py): a value the AI returned must be printed in the document text."""

from __future__ import annotations

from app.modules.extraction.grounding import ungrounded_fields

TEXT = ["""TAX INVOICE
Demo Traders Pvt Ltd   GSTIN: 29ABCCE1234F1ZY
Invoice No: INV-2026-0042   Invoice Date: 7th September 2026   Due: 07/10/2026
Steel brackets   10   1,000.00   10,000.00
Total: Rs. 17,700.00"""]


def _result(**overrides):
    base = {"invoice_number": "INV-2026-0042", "invoice_date": "2026-09-07", "due_date": "2026-10-07",
            "seller": {"name": "Demo Traders Pvt Ltd", "gstin": "29ABCCE1234F1ZY"},
            "line_items": [{"description": "Steel brackets", "quantity": "10.00", "rate": "1000.00",
                            "amount": "10000.00"}],
            "total": "17700.00"}
    base.update(overrides)
    return base


def test_a_correct_result_is_fully_grounded():
    assert ungrounded_fields(_result(), TEXT) == []


def test_dates_match_in_any_printed_form():
    # "7th September 2026" (ordinal, long month) and "07/10/2026" (numeric) both ground ISO dates.
    assert ungrounded_fields({"invoice_date": "2026-09-07", "due_date": "2026-10-07"}, TEXT) == []


def test_money_matches_regardless_of_separators_and_zeros():
    assert ungrounded_fields({"total": "17700.00"}, TEXT) == []
    assert ungrounded_fields({"total": "1000.00"}, TEXT) == []        # printed as 1,000.00
    assert ungrounded_fields({"total": "10.00"}, TEXT) == []          # the quantity 10


def test_one_wrong_digit_in_an_amount_is_caught_and_named():
    assert ungrounded_fields(_result(total="17800.00"), TEXT) == ["total"]


def test_one_wrong_character_in_an_identifier_is_caught():
    assert ungrounded_fields(_result(seller={"name": "Demo Traders Pvt Ltd", "gstin": "29ABCCE1234F1ZX"}),
                             TEXT) == ["seller.gstin"]


def test_a_wrong_date_is_caught():
    assert ungrounded_fields(_result(invoice_date="2026-09-08"), TEXT) == ["invoice_date"]


def test_invented_value_inside_a_list_is_caught_with_its_position():
    result = _result(line_items=[{"description": "Steel brackets", "amount": "10000.00"},
                                {"description": "Gold plating", "amount": "99999.00"}])
    assert ungrounded_fields(result, TEXT) == ["line_items[1].description", "line_items[1].amount"]


def test_case_spacing_and_punctuation_do_not_matter_for_text():
    assert ungrounded_fields({"seller": {"name": "DEMO  TRADERS pvt. ltd"}}, TEXT) == []


def test_enum_like_fields_and_empty_values_are_skipped():
    assert ungrounded_fields({"id_type": "pan", "gender": "M", "pep_status": "No", "invoice_number": None}, TEXT) == []


def test_values_are_never_returned_only_paths():
    out = ungrounded_fields(_result(total="12345.67"), TEXT)
    assert out == ["total"] and "12345" not in "".join(out)
