# WI-ET-020 Electrical Test Failure Triage

> Synthetic, for demo only. Invented for a home-lab project; not a real procedure.

Revision: B. Owner: Test Engineering. Applies to: flying probe and fixture testers.

## Failure types

- Open: a net has a resistance above the continuity limit (20 ohm for standard orders).
- Short or leakage: the isolation resistance between two nets is below the limit (10 megaohm).

## Triage steps

1. Retest the failing board once. Many first-time fails are caused by poor probe contact.
2. If it fails again, clean the pads at the failing points with an approved wipe and retest.
3. Check that the netlist revision on the tester matches the CAM data revision on the order traveler. A wrong netlist causes fails on many boards at once.
4. Use the net coordinates from the tester report to find the failure location, and inspect it under the microscope.
5. Classify the result:
   - Real open or short: log a defect in the MES with station "Electrical Test" and defect type Open or Short.
   - Contact problem or probe mark: log it as a false fail, not as a defect.

## Repair rules

- Opens are never repaired with wire jumpers in this plant.
- Outer-layer shorts may be repaired only on Industrial orders, and only if the traveler allows repair.
- No repair is allowed on Defense, Aerospace or Medical orders. These boards are scrapped.

## When to escalate

- If the same net fails on 3 or more boards in a lot, stop testing that lot and notify the process engineer. The cause is usually in imaging, etching or plating.
- If more than 5% of the boards in a lot fail, put the order on hold with reason code Q according to WI-PLN-007.
- The tester self-test must pass at the start of every shift. If it fails, do not test boards; call the test technician.
