# WI-PLN-007 Order Hold and Release

> Synthetic, for demo only. Invented for a home-lab project; not a real procedure.

Revision: D. Owner: Production Planning. Applies to: all production orders.

## Who may place a hold

Shift managers, QA engineers and planners may put an order on hold. Operators ask their shift manager.

## Reason codes

- Q: quality issue (for example delamination, repeated defects, failed thermal stress test)
- M: material shortage or material quality problem
- C: customer request or missing customer data
- E: engineering query (CAM data, drawing or stack-up question)

## Placing an order on hold

1. Set the order status to On Hold in the MES and choose the reason code. Add a short description.
2. Attach a red HOLD tag to the panels and move them to the quarantine or hold location named in the MES.
3. Inform the planner. If the due date is within 5 working days, the planner informs customer service the same day.

## Daily review

- All open holds are reviewed at the 09:00 production meeting.
- Holds older than 3 working days are escalated to the plant manager.

## Releasing an order

- Quality holds (Q) may be released only by a QA engineer, and only after the Material Review Board decision is recorded.
- Material, customer and engineering holds (M, C, E) are released by the planner when the cause is resolved.
- On release, set the status back to the status before the hold (normally In Production or In QA), remove the HOLD tag, and record who released the order and why.
- If the hold delayed the order, the planner agrees a new due date with customer service and updates the MES.

## Never

- Never ship an order that is On Hold.
- Never remove a HOLD tag without releasing the order in the MES.
