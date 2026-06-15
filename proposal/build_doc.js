const fs = require('fs');
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  AlignmentType, HeadingLevel, BorderStyle, WidthType, ShadingType, PageNumber,
  Header, Footer, VerticalAlign,
} = require('docx');

const BRAND = "017E84";
const C = { in: "DDF3E6", cust: "FCEFD0", part: "DCEBF5", con: "F7D9D9", head: BRAND };
const PAGE_W = 12240, MARGIN = 1080, CONTENT = PAGE_W - 2 * MARGIN; // 10080

const thin = { style: BorderStyle.SINGLE, size: 1, color: "C9D3D7" };
const borders = { top: thin, bottom: thin, left: thin, right: thin };

function cell(text, w, opts = {}) {
  const runs = Array.isArray(text) ? text : [new TextRun({ text: String(text), bold: !!opts.bold, color: opts.color || "1F2A30", size: opts.size || 19 })];
  return new TableCell({
    width: { size: w, type: WidthType.DXA }, borders,
    shading: opts.fill ? { fill: opts.fill, type: ShadingType.CLEAR } : undefined,
    margins: { top: 60, bottom: 60, left: 110, right: 110 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({ children: runs, spacing: { after: 0 } })],
  });
}

const COLS = [4380, 1500, 1400, 2800]; // Requirement, Status, Effort, Notes  (sum 10080)
function headerRow() {
  return new TableRow({ tableHeader: true, children: [
    cell([new TextRun({ text: "Requirement", bold: true, color: "FFFFFF", size: 19 })], COLS[0], { fill: C.head }),
    cell([new TextRun({ text: "Status", bold: true, color: "FFFFFF", size: 19 })], COLS[1], { fill: C.head }),
    cell([new TextRun({ text: "Effort*", bold: true, color: "FFFFFF", size: 19 })], COLS[2], { fill: C.head }),
    cell([new TextRun({ text: "Notes", bold: true, color: "FFFFFF", size: 19 })], COLS[3], { fill: C.head }),
  ]});
}
const STAT = {
  IN:   { label: "In system",    fill: C.in },
  CU:   { label: "Customizable", fill: C.cust },
  PA:   { label: "Partial",      fill: C.part },
  CON:  { label: "Constraint",   fill: C.con },
};
function row(req, statusKey, effort, notes) {
  const s = STAT[statusKey];
  return new TableRow({ children: [
    cell(req, COLS[0]),
    cell([new TextRun({ text: s.label, bold: true, size: 18 })], COLS[1], { fill: s.fill }),
    cell([new TextRun({ text: effort, size: 18 })], COLS[2]),
    cell([new TextRun({ text: notes, size: 18, color: "44515A" })], COLS[3]),
  ]});
}
function sectionTable(rows) {
  return new Table({ width: { size: CONTENT, type: WidthType.DXA }, columnWidths: COLS,
    rows: [headerRow(), ...rows] });
}
function h2(t) { return new Paragraph({ heading: HeadingLevel.HEADING_2, spacing: { before: 260, after: 120 }, children: [new TextRun(t)] }); }
function p(t, opts = {}) { return new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text: t, size: 20, ...opts })] }); }

const children = [];

// Title block
children.push(new Paragraph({ spacing: { after: 40 }, children: [new TextRun({ text: "Shopify ⇄ Odoo Connector", bold: true, size: 40, color: BRAND })] }));
children.push(new Paragraph({ spacing: { after: 40 }, children: [new TextRun({ text: "Requirement Compatibility Assessment", bold: true, size: 26, color: "1F2A30" })] }));
children.push(new Paragraph({ spacing: { after: 200 }, children: [new TextRun({ text: "Prepared by Nepsol Web  ·  nepsolweb.com", size: 18, color: "5B6B73" })] }));

children.push(p("This document maps each of your stated requirements to the current connector and indicates, for items not already present, the customisation effort to make them compatible. Nothing in your environment has been changed to produce this assessment.", { italics: true, color: "44515A" }));

// Legend
children.push(h2("How to read this"));
const legend = new Table({ width: { size: CONTENT, type: WidthType.DXA }, columnWidths: [2400, 7680], rows: [
  new TableRow({ children: [ cell([new TextRun({ text: "In system", bold: true, size: 18 })], 2400, { fill: C.in }), cell("Available today (may need configuration only).", 7680) ]}),
  new TableRow({ children: [ cell([new TextRun({ text: "Partial", bold: true, size: 18 })], 2400, { fill: C.part }), cell("Core capability exists; some of the requested behaviour needs customisation.", 7680) ]}),
  new TableRow({ children: [ cell([new TextRun({ text: "Customizable", bold: true, size: 18 })], 2400, { fill: C.cust }), cell("Not present today, but feasible to build on the existing framework.", 7680) ]}),
  new TableRow({ children: [ cell([new TextRun({ text: "Constraint", bold: true, size: 18 })], 2400, { fill: C.con }), cell("External dependency / blocker — see note.", 7680) ]}),
]});
children.push(legend);

// Critical constraints
children.push(h2("Critical compatibility notes (read first)"));
children.push(new Paragraph({ numbering: { reference: "n", level: 0 }, children: [new TextRun({ text: "Odoo version: the connector is built for Odoo 19. Your environment is Odoo.sh 17. It will not install on 17 as-is — it requires a back-port to 17, or deployment after your planned Odoo 19 upgrade. This is a prerequisite for everything below.", size: 20 })] }));
children.push(new Paragraph({ numbering: { reference: "n", level: 0 }, children: [new TextRun({ text: "Shopify Basic plan: most requirements are unaffected, but Store-Credit refunds and Order-editing depend on what the Basic plan exposes via the Shopify API and should be confirmed with Shopify before commitment.", size: 20 })] }));
children.push(new Paragraph({ numbering: { reference: "n", level: 0 }, children: [new TextRun({ text: "Real-time sync requires a public HTTPS endpoint for webhooks — Odoo.sh provides this.", size: 20 })] }));

// Sections
children.push(h2("1. Shopify → Odoo Sales import"));
children.push(sectionTable([
  row("a. Import orders from 2 stores (ZA / EU)", "IN", "—", "One connection per store (multi-instance)."),
  row("b. Match products by SKU", "IN", "—", "SKU matching strategy built in."),
  row("c. Import customer name, phone, email, addresses", "IN", "—", "Built."),
  row("d. Import order notes & tags onto the SO", "PA", "0.5 d", "Imported to the order record; mapping onto sale.order fields = small work."),
  row("e. Use Shopify order # as the Odoo SO number (override sequence)", "CU", "0.5 d", "Today stored as Source Document + note; overriding SO name is a small change."),
  row("f. Auto-confirm SO (Quotation → Sale)", "IN", "—", "Order Workflow option."),
  row("g. Store prefix → sales_order_prefix field", "CU", "1 d", "Read Shopify Order-ID prefix and map to a new field."),
  row("g-viii. Import only Paid+Unfulfilled; hold Unpaid until paid; skip Fulfilled", "PA", "1 d", "Status filtering exists; exact rule + re-import on paid (webhook) = config/customisation."),
  row("h. Prices from the Shopify order, no pricelist override", "IN", "—", "Line prices imported directly."),
  row("i. Tax applied/not, mapped correctly", "PA", "1.5 d", "Tax data imported; VAT→Odoo-tax mapping setup required."),
  row("j. No automated invoice", "IN", "—", "Invoice automation is optional (off)."),
]));

children.push(h2("2. Pricing rules & tax handling"));
children.push(sectionTable([
  row("a–c. All pricing/totals from the order only", "IN", "—", "Built; no pricelist applied."),
  row("d/e. VAT present → apply; absent → none", "CU", "(incl. 1i)", "Logic to mirror the order's VAT exactly."),
  row("f. Up to SO stage, no invoice", "IN", "—", "Built."),
]));

children.push(h2("3. Odoo SO edits → Shopify"));
children.push(sectionTable([
  row("Add products to the Shopify order", "IN", "—", "orderEdit add-variant + commit built."),
  row("Change quantity / remove products", "CU", "2 d", "Add set-quantity & line-removal to the edit flow."),
  row("Push edits automatically on Odoo save", "CU", "1 d", "Trigger on SO write (today action-based)."),
  row("Order-edit email from Shopify", "IN", "—", "notifyCustomer on commit."),
  row("(Automated credit note) ", "IN", "—", "Optional auto credit-note is available."),
]));

children.push(h2("4. Odoo cancel & refund → Shopify"));
children.push(sectionTable([
  row("Cancel, refund, partial refund", "IN", "—", "orderCancel + two-way refundCreate (partials supported)."),
  row("Push automatically on Odoo action", "CU", "1 d", "Auto-trigger on cancel/refund."),
  row("Refund as Store Credit", "CU", "1.5 d", "API method exists; verify availability on Shopify Basic."),
  row("Cancel/refund email from Shopify", "IN", "—", "notifyCustomer."),
  row("No invoice automation", "IN", "—", "Manual default preserved."),
]));

children.push(h2("5. Fulfilment & tracking → Shopify"));
children.push(sectionTable([
  row("Push tracking, mark fulfilled, correct store", "IN", "—", "fulfillmentCreate with tracking, per store."),
  row("Archive the Shopify order after fulfilment", "CU", "0.5 d", "Add order-close after fulfilment."),
  row("Fulfilment email from Shopify", "IN", "—", "notifyCustomer."),
  row("Multiple / split shipments", "PA", "2 d", "Fulfilment-order model supports partials; full split UX = customisation."),
]));

children.push(h2("6. Bi-directional multi-store stock sync"));
children.push(sectionTable([
  row("Mesh sync: Odoo ⇄ all stores kept identical (sale in ZA → Odoo → EU)", "PA", "2.5 d", "Inventory push + webhooks exist; the cross-store fan-out needs wiring. Requires location mapping per store."),
]));

children.push(h2("7. Notification sync (emails only on edit / cancel / refund)"));
children.push(sectionTable([
  row("Restrict Shopify customer emails to edit, cancel, refund", "IN", "0.5 d", "Per-action notify flags; set so only these three notify."),
]));

// Summary
children.push(h2("Summary & indicative effort"));
children.push(p("Approximately 60% of the specification is already present in the connector. The remaining items are standard customisations on the existing framework. Indicative customisation effort (excluding the Odoo 17 prerequisite): ~13–15 developer-days, subject to a short discovery phase.", {}));
children.push(p("Prerequisite — Odoo 17 back-port: ~4–6 developer-days (or zero if deployed after your Odoo 19 upgrade).", { bold: true }));

children.push(new Paragraph({ spacing: { before: 160 }, children: [new TextRun({ text: "* Effort figures are indicative estimates for scoping only and are confirmed after a requirements/discovery session. They assume the connector runs on a compatible Odoo version and that the Shopify plan exposes the required APIs.", italics: true, size: 16, color: "5B6B73" })] }));

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 20, color: "1F2A30" } } },
    paragraphStyles: [
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, color: BRAND, font: "Arial" },
        paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 1 } },
    ],
  },
  numbering: { config: [
    { reference: "n", levels: [{ level: 0, format: "decimal", text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 520, hanging: 300 } } } }] },
  ]},
  sections: [{
    properties: { page: { size: { width: PAGE_W, height: 15840 }, margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN } } },
    footers: { default: new Footer({ children: [ new Paragraph({ alignment: AlignmentType.CENTER, children: [
      new TextRun({ text: "Nepsol Web — Shopify ⇄ Odoo Connector · Confidential · Page ", size: 16, color: "8a969c" }),
      new TextRun({ children: [PageNumber.CURRENT], size: 16, color: "8a969c" }),
    ]})]})},
    children,
  }],
});

Packer.toBuffer(doc).then(b => { fs.writeFileSync("Shopify_Odoo_Connector_Compatibility.docx", b); console.log("written", b.length, "bytes"); });
