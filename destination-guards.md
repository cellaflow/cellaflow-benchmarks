# What the destination does about a repeated write

Every benchmark in this repository measures an agent. This page is about the
other end of the call: when an agent writes a sales order into an ERP twice,
what does the ERP itself do about it?

**This page is reading, not measurement.** One row is tested. The rest comes
from vendor documentation and from the communities around each product, and
each claim links to where it came from. Treat it as a map of where to look
rather than as a result, and tell us where it is wrong.

## The table

| destination | idempotency key on the order write | duplicate customer-PO guard | what the caller gets on a repeat |
| :--- | :--- | :--- | :--- |
| [ERPNext 16.53](erp-order-entry/) | none | yes, ships enabled | a validation error, no order number |
| QuickBooks Online | yes, `requestid`, 24 hour window | n/a | **the original response, replayed** |
| NetSuite | yes, asynchronous requests only | not on Customer PO# by default | 400, plus a `Location` header naming the first job |
| SAP S/4HANA | none found | yes, per sales document type, configurable | a warning message |
| Dynamics 365 Business Central | none found | no | a second order |
| Odoo | none found | no | a second order |
| Acumatica | none found | none found | unknown |

Only the ERPNext row was run. It is the subject of
[`erp-order-entry/`](erp-order-entry/), where the numbers and the harness are.

## The gap

**One destination out of seven hands the caller back the first result.**

That is the behaviour that finishes the job. The agent learns the work is
already done, gets the identifier of the record that exists, and can mark its
own task complete. QuickBooks Online does it. NetSuite comes closest on the
asynchronous path, where the `Location` header at least says where the first
answer went.

Everything else either writes the second order or refuses it. A refusal sounds
like the safe outcome and it is not the same thing as an answer: the caller is
told no, is not told what exists, and has no basis for deciding whether to
treat the task as finished. The ERPNext benchmark measures what that costs.
Across five runs of two workers on one inbox it produced ten refusals per run,
correct orders in the ERP, and ten jobs that never closed.

Three things follow.

**A key on the wrong path is not a key.** NetSuite's REST services run
synchronously unless the caller opts into asynchronous execution, and the
idempotency key is documented for the asynchronous path. An ordinary
`POST /record/v1/salesOrder` is the call most integrations make, and the header
does not apply to it. Using the key means changing the shape of the
integration, not adding a header.

**Two widely deployed destinations have no guard at all.** Odoo and Business
Central will both accept the second order. Odoo's `client_order_ref` carries no
uniqueness constraint and the community answer is to write an `@api.constrains`
yourself. Business Central does not check external document numbers for
uniqueness, though `SearchForExternalDocNo` in codeunit 1302 exists to be wired
up, matching within one customer. Where the destination does nothing, the
calling side is the only thing between a crashed worker and a duplicate.

**A configurable warning is a property of an installation, not of a product.**
SAP's check lives on the sales document type and can warn, block or do nothing
depending on how it was customised. "SAP checks this" is not answerable in
general. Ask which setting is on in the system in front of you.

## What none of them cover

A crash between the destination committing and the caller holding any record
of it. Whatever the destination offers, the retry has to present the same key
or the same reference, which means that value had to be derived before the
call and survive the process that made it. The record and the side effect are
in different systems and a process can stop between them. The
[`erp-order-entry/`](erp-order-entry/) table reports that row rather than
leaving it out, and no arm in it closes that row, including ours.

## Where each row came from

- **ERPNext.** `validate_po` in `erpnext/selling/doctype/sales_order/sales_order.py`
  at v16.53.0, gated by `Selling Settings.allow_against_multiple_purchase_orders`,
  which ships unchecked. Read at the pin and measured in
  [`erp-order-entry/`](erp-order-entry/).
- **QuickBooks Online.** The `requestid` query parameter, which Intuit
  documents as letting an app resend an operation after a service disruption,
  with the service replaying the original response rather than creating a
  second record.
  [Write-up](https://www.codeproject.com/Articles/1083765/Idempotent-Quickbooks-Online-integrations-Reques).
- **NetSuite.** [Sending an asynchronous request using an idempotency key](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_164494900632.html),
  Oracle NetSuite help centre. The key is documented under asynchronous
  execution; REST services are synchronous by default.
- **SAP S/4HANA.** The `Check purch.order no.` field on the sales document type
  in VOV8, and SAP note
  [1723947, Duplicate PO Number Check V4115](https://userapps.support.sap.com/sap/support/knowledge/en/1723947).
  No idempotency facility found for `API_SALES_ORDER_SRV`.
- **Dynamics 365 Business Central.** [Community discussion of duplicate external document numbers](https://www.dynamicsuser.net/t/how-to-avoid-entry-of-duplicate-external-document-number-in-sales-order/52851)
  and Microsoft's [external document numbers](https://learn.microsoft.com/en-us/dynamics365/business-central/across-enter-external-document-numbers)
  documentation.
- **Odoo.** [Odoo forum answer on preventing duplicate client references](https://www.odoo.com/forum/help-1/automation-help-to-prevent-client-reference-duplicates-222508),
  which recommends adding the constraint yourself. Not yet verified against
  `sale_order.py` at a pinned tag, which is the check that would settle it.
- **Acumatica.** Nothing found either way. "None found" here means the search
  came up empty, not that the product lacks it.

## Corrections wanted

Four of these rows would change on one link to a vendor document, and the
Acumatica row is empty. If you integrate any of these for a living and a row
is wrong, open an issue. The useful correction is the one that says which call
you make and what it returned the second time.
