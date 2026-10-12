# WI-SM-015 Solder Mask Touch-Up Limits

> Synthetic, for demo only. Invented for a home-lab project; not a real procedure.

Revision: C. Owner: Quality Engineering. Applies to: finished boards with solder mask skips, voids or scratches.

## General rules

- Use only the approved touch-up material, with the same colour and type as specified on the traveler.
- Cure the touch-up exactly as written in the material datasheet (UV or thermal).
- Record every touch-up in the touch-up log: board serial number, side, location and operator.

## Size and count limits

| Customer sector | Max touch-ups per board side | Max size of one touch-up |
|---|---|---|
| Industrial | 3 | 5 mm x 5 mm |
| Medical | 2 | 5 mm x 5 mm |
| Defense, Aerospace | 1, and only if the traveler says "SM touch-up allowed" | 3 mm x 3 mm |

## Where touch-up is never allowed

- On pads, SMD lands or gold fingers, or closer than 0.25 mm to them.
- On impedance-controlled traces marked on the traveler.
- In via-in-pad areas.

## After touch-up

1. Inspect every touched-up area under 10x magnification after curing.
2. Do a tape adhesion test on 1 board per lot.
3. If the defect is larger than the limits, or in a forbidden area, reject the board and log a defect with type "Solder Mask Defect".
4. Do not strip and recoat solder mask without approval from the solder mask process engineer.
