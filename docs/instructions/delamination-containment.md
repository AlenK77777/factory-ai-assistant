# WI-QA-031 Delamination Containment

> Synthetic, for demo only. Invented for a home-lab project; not a real procedure.

Revision: C. Owner: Quality Engineering. Applies to: all multilayer, rigid-flex and HDI orders.

## When this applies

Use this instruction when delamination or blistering (separation between layers, or between laminate and copper) is found at any station: lamination, drilling, plating, solder mask cure, thermal stress test or Final QA.

## Immediate actions (first 30 minutes)

1. Stop processing the affected lot. Do not rework, re-press or bake the panels.
2. Attach a red HOLD tag to every affected panel and move the lot to quarantine rack Q2.
3. Record the order ID, lot number, panel serial numbers, station and a photo of the defect.
4. Set the order status to On Hold in the MES with reason code Q (quality). See WI-PLN-007 Order Hold and Release.
5. Notify the shift QA engineer within 30 minutes, and inform the lamination process engineer.

## Containment scope

Delamination usually affects more than one panel. Also put on hold:

- all panels from the same lamination press load (check the press load record);
- other open orders that used the same prepreg lot;
- panels from the same load that already moved to later stations.

## Investigation

- Take microsections from 2 panels at the affected location to confirm delamination and measure its size.
- Check the press cycle record: temperature profile, pressure and vacuum.
- Check material storage: prepreg age and storage humidity.
- Moisture is the most common cause. Check that inner layers were baked before lamination (2 hours at 120 °C, according to WI-LAM-004).

## Disposition

- The Material Review Board (MRB) decides within 1 working day.
- For Defense, Aerospace and Medical orders, delaminated boards are always scrapped. "Use as is" is not allowed.
- For Industrial orders, "use as is" is allowed only with the customer's written approval.
- Only the QA engineer may release the hold after the MRB decision.
