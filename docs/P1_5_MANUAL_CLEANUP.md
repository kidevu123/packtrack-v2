# P1.5 — Manual cleanup list

The 49 packaging items below have **no `sku_code` in Zoho**, so the
`scripts/audit_material_codes.py --apply-safe-defaults` run could not
propose a value for them. Each one needs a `material_code` chosen by the
owner before it can flow to Luma in P5.

**Why these can't be auto-filled:**

- The audit refuses to propose a `material_code` when there's nothing
  unambiguous to copy from. Generating one (e.g. from `name`) would risk
  creating a code Luma later collides with.
- `zoho_item_id` is technically unique but opaque (long Zoho-internal
  string). It is *not* recommended as a customer-facing material code.

**Recommended action for each:**

1. Open the item in Zoho Inventory → set the **SKU** field to the real
   product code (UPC, vendor SKU, etc.). Then run
   `python scripts/sync_items_via_gateway.py` again to refresh
   PackTrack's `sku_code`, then `python scripts/audit_material_codes.py
   --apply-safe-defaults`.
2. **Or** set `material_code` directly in PackTrack via SQL or admin
   tooling once that lands. The partial unique index will reject any
   collision automatically.

**Snapshot taken at P1.5 close (2026-05-09).** Counts may shift as
operators clean up Zoho.

---

## Items requiring a material_code

| PackTrack id | zoho_item_id | name |
|---:|---|---|
| 1 | 5254962000000106075 | 25ct Master Case Box [Packaging] |
| 2 | 5254962000000106463 | 50ct Master Case Box [Packaging] |
| 3 | 5254962000000322432 | FIX 12ct Hybrid Focus (Green) - Display Box [Packaging] |
| 4 | 5254962000000322405 | FIX 15mg 12ct Hybrid Focus (Green) - Bottle Label [Packaging] |
| 5 | 5254962000000322396 | FIX 15mg 12ct Pseudo Relax (Red) - Bottle Label [Packaging] |
| 6 | 5254962000000322441 | FIX 15mg 12ct Pseudo Relax (Red) - Display Box [Packaging] |
| 7 | 5254962000000322414 | FIX 15mg 12ct Regular Energy (White) - Bottle Label [Packaging] |
| 8 | 5254962000000322423 | FIX 15mg 12ct Regular Energy (White) - Display Box [Packaging] |
| 9 | 5254962000000679302 | FIX 20mg 12ct Assorted - Display Box [Packaging] |
| 11 | 5254962000000679626 | FIX 30mg 5ct Focus (Green) - Blister Card [Packaging] |
| 13 | 5254962000000679507 | FIX 50mg 1ct Energy (White) - Mylar Bag [Packaging] |
| 14 | 5254962000000679524 | FIX 50mg 1ct Focus (Green) - Mylar Bag [Packaging] |
| 15 | 5254962000001258423 | FIX 50mg 1ct Master Case Box [Packaging] |
| 16 | 5254962000001245178 | FIX 5ct Master Case Box [Packaging] |
| 17 | 5254962000000679558 | FIX Energy (White) 1ct - 20ct Draw Box PHOTO [Packaging] |
| 18 | 5254962000000679660 | FIX Energy (White) 5ct - Display Box [Packaging] |
| 19 | 5254962000000679575 | FIX Focus (Green) 1ct - 20ct Draw Box [Packaging] |
| 20 | 5254962000000679677 | FIX Focus (Green) 5ct - Blister Display Box [Packaging] |
| 21 | 5254962000005277161 | FIX Her Strips - Display Box [Packaging] |
| 22 | 5254962000005277192 | FIX Her Strips Mylar Bag [Packaging] |
| 27 | 5254962000000679592 | FIX Relax (Red) 1ct - 20ct Draw Box [Packaging] |
| 28 | 5254962000000679541 | FIX Relax (Red) 1ct - Mylar Bag [Packaging] |
| 29 | 5254962000000679694 | FIX Relax (Red) 5ct - Blister Display Box [Packaging] |
| 39 | 5254962000000106241 | Hyroxi 1CT 7OH - Blister Card + Blister [Packaging] |
| 40 | 5254962000000106258 | Hyroxi 1ct 7OH - Display w/ Green Holder [Packaging] |
| 42 | 5254962000000115249 | Hyroxi 4ct - Blister Card [Packaging] PHOTO DUPLICATE DONT USE |
| 44 | 5254962000000177640 | Hyroxi 5ct Hybrid - Blister Card + Blister [Packaging] |
| 45 | 5254962000000177649 | Hyroxi 5ct Hybrid - Display Box [Packaging] |
| 46 | 5254962000000177613 | Hyroxi 5ct Pseudo - Blister Card + Blister [Packaging] |
| 47 | 5254962000000177622 | Hyroxi 5ct Pseudo - Display Box [Packaging] |
| 50 | 5254962000000115271 | Hyroxi 7OH 4ct Display Box PHOTO [Packaging] |
| 52 | 5254962000001545053 | Hyroxi Shot GA Compliant 30ml - Master Case Box [Packaging] |
| 54 | 5254962000001270281 | Hyroxi Shot GA Compliant 60ml - Display Box [Packaging] |
| 55 | 5254962000001270260 | Hyroxi Shot GA Compliant 60ml - Wrap [Packaging] |
| 59 | 5254962000001270232 | Hyroxi Shots 30ml Master Case Box [Packaging] |
| 60 | 5254962000001270302 | Hyroxi Shots 60ml Master Case Box [Packaging] |
| 63 | 5254962000001545074 | Hyroxi Shots GA Compliant 60ml - Master Case Box [Packaging] |
| 66 | 5254962000000691412 | Hyroxi XL 1ct - Display Box [Packaging] |
| 68 | 5254962000000691487 | Hyroxi XL 4ct - Blister Card [Packaging] |
| 71 | 5254962000000691778 | Hyroxi XL 7ct - Blister Display Box [Packaging] |
| 75 | 5254962000000691525 | Hyroxi XL Hybrid 5ct - Blister [Packaging] |
| 86 | 5254962000001053103 | KAIZEN Shot 60ml - Master Case Box PHOTO [Packaging] |
| 87 | 5254962000001053069 | KAIZEN Shot 60ml Coco Zen - Display Box [Packaging] |
| 88 | 5254962000001053001 | KAIZEN Shot 60ml Coco Zen - Shrink Wrap [Packaging] |
| 89 | 5254962000001053052 | KAIZEN Shot 60ml Tokyo Drift - Display Box [Packaging] |
| 90 | 5254962000001053018 | KAIZEN Shot 60ml Tokyo Drift - Shrink Wrap [Packaging] |
| 91 | 5254962000001053086 | KAIZEN Shot 60ml Yuzu Rush - Display Box [Packaging] |
| 92 | 5254962000001053035 | KAIZEN Shot 60ml Yuzu Rush - Shrink Wrap [Packaging] |
| 94 | 5254962000000177568 | Shots Master Case Box [Packaging] |
