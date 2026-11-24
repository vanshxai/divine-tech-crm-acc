# Divine Books: what Busy does, and what we keep

Sources: Busy's own pages ([features](https://busy.in/busy-features-and-faqs), [voucher types](https://busy.in/accounting/what-is-a-voucher-benefits-types-and-components/), [GST](https://busy.in/gst-accounting-software), [reconciliation](https://busy.in/accounting-software/gstr-reconciliation/)). Field lists marked *(estimate)* come from general Indian accounting practice and must be checked against your real bills.

## 1. How accounting works, in one minute

Every money event is a **voucher**: a dated entry that moves money between **accounts** (ledgers). Each voucher has equal debit and credit, so the books always balance. That is the "double-entry ledger" already built into Divine Books. Busy is a big machine for entering vouchers, then reporting on them.

## 2. What Busy does

**Masters** (set up once)
- Accounts/ledgers: customers, suppliers, bank, cash, sales, purchases, expenses, GST accounts
- Items: name, unit, HSN code, GST rate, opening stock
- Groups, units, godowns, GST rates, financial year

**Vouchers**: three families
| Family | Types | Effect |
|---|---|---|
| Accounting only | Payment, Receipt, Contra (cash↔bank), Journal, Debit/Credit Note without items | Money only |
| Stock only | Stock Journal, Stock Transfer, Material issue/receive, Production | Stock only |
| Both | **Sales, Purchase, Sales Return, Purchase Return** | Money and stock |

**Reports**: Day Book, Ledger, Trial Balance, Profit & Loss, Balance Sheet, Outstanding (who owes us / whom we owe), Stock Summary, Sales/Purchase registers, Cash/Bank book.

**GST and compliance**: GSTR-1 and GSTR-3B data, GSTR-2A/2B matching against purchases (ITC check), e-invoice, e-way bill, GSTIN and HSN validation, RCM.

**Also in Busy**: quotations/orders, WhatsApp share, multi-user rights, backup, multi-company, bank reconciliation, TDS (unconfirmed), costing and production, barcodes.

## 3. What your bills will probably need

Busy's input screens, estimated from standard Indian practice. Your sample bills decide the final list.

**Purchase bill (from a supplier)** *(estimate)*
- Date, voucher no, supplier, supplier invoice no and date, supplier GSTIN, state (place of supply)
- Per item: name, HSN, qty, unit, rate, discount, GST %
- Freight/other charges, taxable total, CGST/SGST or IGST, round-off, bill total
- Payment: cash/bank/credit (pay later), narration, e-way bill no if goods moved

**Sales bill (ours)**: already built. Missing: discount, freight, HSN, units, PDF/WhatsApp share, sales return.

**Payment slip / bank entry** *(estimate)*: date, account, amount, mode, reference, against which bill.

**Expenses** (fuel, rent, electricity, salary, petrol): date, expense head, amount, paid by, bill photo.

## 4. The 20% we build

**Tier 1: must have**
1. Clients and suppliers (one "parties" list)
2. Sales bill (done) plus discount, HSN, units
3. **Purchase bill** (next)
4. Payment received / payment made
5. Expenses
6. Reports: outstanding, day book, ledger, trial balance, P&L, sales and purchase register
7. GST summary for the CA: output tax minus input tax, GSTR-1 and 3B data as Excel
8. Reversal instead of delete (done)

**Tier 2: soon**: stock (items, in/out per job), quotations, sales return / credit note, bank statement import and matching, Balance Sheet, GSTIN checksum check, WhatsApp share.

**Tier 3: skip until needed**: production, barcodes, multi-company, TDS, costing, payroll. Revisit e-invoice if turnover crosses ₹5 crore.

## 5. Photo bill → books (Telegram)

```
Photo in Telegram → server → read text + fields → DRAFT purchase bill → human taps Approve → posts to ledger
```
Rules that keep it safe:
1. **A draft, never an auto-post.** Mistaken reads are normal. Approving takes one tap.
2. Check automatically: GSTIN checksum, tax maths (items × rate = total), duplicate (same supplier GSTIN + invoice no), amount mismatch. Flag the weak fields in red.
3. Keep the original photo attached to the voucher forever (audit trail).
4. The Telegram bot you have already is the front door. The new part is the reader and the draft screen.
5. Reading method (OCR vs a vision model) is a cost-and-accuracy test, which we do with your real bills. Nothing is decided.

## 6. CRM connection

One database, shared parties. Job → quote → bill → payment follows one client record. Purchase bills can link to a job, so job profit is visible. Stock used on a job links to the employee (your earlier plan).

## 7. Gujarati for your mother

- Every label shows **English with Gujarati under it**, from one translation file, so labels are never mixed up in the code.
- Plain words: ગ્રાહક (customer), બિલ (bill), ખરીદી (purchase), વેચાણ (sale), ચુકવણી (payment), બાકી (pending), ખર્ચ (expense).
- No accounting words on screen ("debit", "ledger") for the daily screens. Use "money in / money out". Technical words stay in Books only.
- She should review the wording. I can translate, but she knows what sounds natural.
- Later option: voice or a Gujarati Telegram flow.

## 8. What I need from you

1. **3 to 5 real sample bills** (purchase bills, expense bills, and a sales bill from Busy). Blur anything private if you like.
2. A **Busy export of last year**: party list, item list, and sales/purchase registers.
3. Who enters bills today (you, your mother, the CA), and what the CA needs at month end.
