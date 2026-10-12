# WI-PLT-009 Plating Void Investigation

> Synthetic, for demo only. Invented for a home-lab project; not a real procedure.

Revision: A. Owner: Plating Process Engineering. Applies to: plated through holes and microvias.

## When this applies

Use this instruction when voids in hole copper are found by microsection, or when Electrical Test finds opens that point to plated holes.

## Immediate actions

1. Put the order on hold with reason code Q according to WI-PLN-007.
2. Notify the plating process engineer and the shift QA engineer.
3. Identify all lots that went through the same electroless copper run in that shift, and hold those that have not been tested yet.

## Investigation

- Take microsections from the test coupons of 2 panels in the lot.
- Measure hole copper thickness. The average must be at least 25 µm for Defense, Aerospace and Medical orders, and at least 20 µm for Industrial orders.
- Check the desmear and electroless copper line records: bath analysis, temperatures and dwell times.
- Check drill quality: rough hole walls and drill hit count can cause voids.
- Check the panel position in the plating rack. Voids that appear only at the rack edges point to agitation or current distribution problems.

## Disposition

- Any void in a Defense, Aerospace or Medical board means the board is rejected.
- For Industrial boards, the Material Review Board decides, using the customer specification.
- Log every rejected board in the MES with station "Plating" and defect type "Plating Void".
