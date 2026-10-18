"""
Accounting & ERP Sync Adapters
Converts verified WhatsApp draft orders into:
1. Tally Prime XML Sales Vouchers (India standard)
2. Xero Accounting REST API Invoices (Global standard)
3. Zoho Books Sales Orders JSON
4. Formatted CSV / Excel nightly batch import
"""
from typing import Dict, Any, List
from xml.sax.saxutils import escape


class AccountingSyncAdapter:
    @staticmethod
    def to_tally_prime_xml(order: Dict[str, Any], tenant: Dict[str, Any]) -> str:
        voucher_date = order.get("date", "20261018").replace("-", "")
        customer_name = escape(order.get("customer_name", "Cash Buyer"))
        order_no = escape(order.get("order_number", "ORD-1001"))

        inventory_entries = []
        for item in order.get("items", []):
            sku_name = escape(item["sku_name"])
            qty = item["qty"]
            unit = escape(item["unit"])
            rate = item["unit_price"]
            amount = item["line_total"]
            inventory_entries.append(f"""
            <ALLINVENTORYENTRIES.LIST>
              <STOCKITEMNAME>{sku_name}</STOCKITEMNAME>
              <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
              <RATE>{rate:.2f}/{unit}</RATE>
              <AMOUNT>{amount:.2f}</AMOUNT>
              <ACTUALQTY> {qty} {unit}</ACTUALQTY>
              <BILLEDQTY> {qty} {unit}</BILLEDQTY>
            </ALLINVENTORYENTRIES.LIST>""")

        joined_entries = "\n".join(inventory_entries)
        return f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{escape(tenant.get("brandName", "Distributor Co"))}</SVCURRENTCOMPANY>
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER VCHTYPE="Sales" ACTION="Create" OBJVIEW="Invoice Voucher View">
            <DATE>{voucher_date}</DATE>
            <VOUCHERNUMBER>{order_no}</VOUCHERNUMBER>
            <PARTYLEDGERNAME>{customer_name}</PARTYLEDGERNAME>
            <NARRATION>Captured via {escape(order.get("channel", "WhatsApp"))} | Raw: {escape(order.get("raw_message", ""))}</NARRATION>
            {joined_entries}
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""

    @staticmethod
    def to_xero_invoice_json(order: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "Type": "ACCREC",
            "Contact": {"Name": order.get("customer_name", "WhatsApp Customer")},
            "Date": order.get("date", "2026-10-18"),
            "DueDate": order.get("due_date", "2026-10-25"),
            "InvoiceNumber": order.get("order_number", "ORD-1001"),
            "Reference": f"WhatsApp Capture ({order.get('channel', 'WABA')})",
            "Status": "DRAFT",
            "LineAmountTypes": "Exclusive",
            "LineItems": [
                {
                    "ItemCode": item["sku_code"],
                    "Description": f"{item['sku_name']} ({item['unit']})",
                    "Quantity": item["qty"],
                    "UnitAmount": item["unit_price"],
                    "TaxType": "OUTPUT",
                    "AccountCode": "200",
                }
                for item in order.get("items", [])
            ],
        }

    @staticmethod
    def to_zoho_books_json(order: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "customer_name": order.get("customer_name"),
            "salesorder_number": order.get("order_number"),
            "date": order.get("date", "2026-10-18"),
            "notes": order.get("delivery_notes", "Captured automatically from WhatsApp"),
            "custom_fields": [
                {"label": "Source Channel", "value": order.get("channel", "WhatsApp")},
                {"label": "Payment Ref", "value": order.get("payment_ref", "Pending")},
            ],
            "line_items": [
                {
                    "sku": item["sku_code"],
                    "name": item["sku_name"],
                    "quantity": item["qty"],
                    "unit": item["unit"],
                    "rate": item["unit_price"],
                    "tax_percentage": item["tax_rate"],
                }
                for item in order.get("items", [])
            ],
        }

    @staticmethod
    def to_csv_batch(orders: List[Dict[str, Any]]) -> str:
        rows = ["OrderNumber,Channel,CustomerName,Phone,SKUCode,ItemName,Qty,Unit,UnitPrice,TaxPercent,LineTotal,GrandTotal,Status"]
        for o in orders:
            for item in o.get("items", []):
                rows.append(
                    f'"{o.get("order_number")}","{o.get("channel")}","{o.get("customer_name")}",'
                    f'"{o.get("customer_phone")}","{item.get("sku_code")}","{item.get("sku_name")}",'
                    f'{item.get("qty")},"{item.get("unit")}",{item.get("unit_price")},'
                    f'{item.get("tax_rate")},{item.get("line_total")},{o.get("grand_total")},"{o.get("status")}"'
                )
        return "\n".join(rows)
