"""Three fictional supplier invoices for the demo, and the scripted replies the TEST DOUBLE reader returns.

Test fixtures, not documents from any account. Supplier names are generic placeholders, addresses and the buyer
are invented; e-mail addresses use the reserved `.example` domain. Amounts and due dates match the planner fixture
in scenario.py.

One invoice is meant to stop for review: the Initech invoice gives its payment time (21:00) with no time zone.
The scripted reply assumes UTC, as a careless reader might; code flags TIMEZONE_NOT_STATED and the console asks a
person to confirm the zone before that invoice reaches the planner.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    obligation_id: str
    text: str


INVOICES = (
    Invoice("inv-acme", "O1", """Acme Co.
billing@acme.example
INVOICE INV-1001
Issued: 27 October 2026
Bill to: Demo Trading Co. (fictional)
Item: corrugated cartons, 400 units
Amount due: EUR 1,200.00
Due by: 3 November 2026, 05:00 UTC
Payment: local bank transfer in EUR
"""),
    Invoice("inv-globex", "O2", """GLOBEX LTD.
Globex Ltd., accounts team
accounts@globex.example
Invoice no. INV-2040
Date: 28 October 2026
Customer: Demo Trading Co. (fictional)
Brass fittings, batch 12
Total payable: GBP 1,000.00
Please pay by 3 November 2026, 15:00 UTC
"""),
    Invoice("inv-initech", "O3", """Initech Pte. Ltd.
ar@initech.example
Invoice INV-3307
Issue date: 29 October 2026
Sold to: Demo Trading Co. (fictional)
Printed sleeves, 2,500 units
Amount due: EUR 2,000.00
Payment due: 4 November 2026, 21:00
"""),
)


def _f(value, quote):
    return [{"value": value, "quote": quote}]


# What the TEST DOUBLE reader "extracts". Scripted by hand; no model produced these. The Initech due date carries
# an assumed +00:00 that its source words do not support, so it goes to review.
SCRIPTED_REPLIES = {
    "inv-acme": {
        "supplier": _f("Acme Co.", "Acme Co."),
        "invoice_number": _f("INV-1001", "INVOICE INV-1001"),
        "amount": _f("1200.00", "Amount due: EUR 1,200.00"),
        "currency": _f("EUR", "EUR 1,200.00"),
        "due_date": _f("2026-11-03T05:00:00+00:00", "Due by: 3 November 2026, 05:00 UTC"),
    },
    "inv-globex": {
        "supplier": _f("Globex Ltd.", "Globex Ltd., accounts team"),
        "invoice_number": _f("INV-2040", "Invoice no. INV-2040"),
        "amount": _f("1000.00", "Total payable: GBP 1,000.00"),
        "currency": _f("GBP", "GBP 1,000.00"),
        "due_date": _f("2026-11-03T15:00:00+00:00", "Please pay by 3 November 2026, 15:00 UTC"),
    },
    "inv-initech": {
        "supplier": _f("Initech Pte. Ltd.", "Initech Pte. Ltd."),
        "invoice_number": _f("INV-3307", "Invoice INV-3307"),
        "amount": _f("2000.00", "Amount due: EUR 2,000.00"),
        "currency": _f("EUR", "EUR 2,000.00"),
        "due_date": _f("2026-11-04T21:00:00+00:00", "Payment due: 4 November 2026, 21:00"),
    },
}
