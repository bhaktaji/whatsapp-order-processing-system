# Indian B2B WhatsApp Commerce & Linguistic Reference Guide

Indian B2B trade across kirana general trade, FMCG wholesale, staples mandis, pharma stockists, and building hardware operates through dense, multilingual shorthand on WhatsApp. This guide documents how OrderBridge models Indian commercial linguistics and GST accounting.

---

## 1. Supported Scripts & Numerals (11 Languages)

OrderBridge normalises numerals and tokenises product aliases across Romanised Hinglish, Devanagari (`०-९`), Gujarati (`૦-૯`), Gurmukhi (`੦-੯`), Bengali (`০-৯`), Tamil (`௦-௯`), Telugu (`౦-౯`), Kannada (`೦-೯`), and Malayalam (`൦-൯`).

---

## 2. Indian Trade Packaging Units (`backend/app/extractor.py`)

| Canonical Unit | Regional & Hinglish Synonyms | Typical Verticals | Conversion Rule |
|---|---|---|---|
| `peti` | `peti`, `petis`, `patia`, `पेटी`, `પેટી`, `ਪੇਟੀ` | Biscuits, Snacks, Beverages, Edible Oil | Master corrugated carton (per-SKU `conversion_factor`, e.g. `1 peti = 24 pcs` or `96 pkt`). |
| `katta` | `katta`, `koli`, `bori`, `bag`, `कट्टा`, `बोरी`, `કટ્ટા` | Rice, Atta, Sugar, Cement | Woven HDPE/jute sack (`25 kg` or `50 kg` SKU variant). |
| `gatta` | `gatta`, `carton`, `ctn`, `box`, `dabba`, `डब्बा` | Confectionery, FMCG, Hardware | Outer carton / shrink-wrapped bundle. |
| `jar` | `jar`, `can`, `tin`, `peepa`, `पीपा`, `जार` | Edible Oils, Ghee, Paints | `15 L` / `15 kg` tin (`peepa`) or `20 L` bucket. |
| `strip` | `strip`, `patti`, `पट्टी` | Pharmaceuticals & OTC | Blister strip (`10` or `15` tablets). |
| `tray` | `tray`, `crate`, `कैरेट` | Dairy, Beverages, Bakery | Returnable plastic crate (`12` or `24` pouches/bottles). |

---

## 3. Compositional Indian Fractions (`fractions.Fraction`)

| Fraction Expression | Script Variants | Exact Rational Value | Decimal Equivalent |
|---|---|---|---|
| `paav` / `pav` | `पाव` / `પાઉ` | $1/4$ | `0.25` |
| `aadha` / `adha` | `आधा` / `અડધો` / `अर्धा` | $1/2$ | `0.50` |
| `paune` (standalone) | `पौने` / `પોણા` | $3/4$ | `0.75` |
| `sawa` (standalone) | `सवा` / `સવા` | $5/4$ | `1.25` |
| `dedh` / `dodh` | `डेढ़` / `દોઢ` / `दीड` | $3/2$ | `1.50` |
| `dhai` / `adhai` | `ढाई` / `અઢી` / `अडीच` | $5/2$ | `2.50` |
| `paune N` ($N \ge 2$) | `पौने N` | $N - 1/4$ | e.g. `paune char` = `3.75` |
| `sawa N` ($N \ge 2$) | `सवा N` | $N + 1/4$ | e.g. `sawa do` = `2.25` |
| `sadhe N` ($N \ge 3$) | `साढ़े N` / `સાડા N` / `साडे N` | $N + 1/2$ | e.g. `sadhe teen` = `3.50` |
