# WI-AOI-012 AOI False-Call Handling

> Synthetic, for demo only. Invented for a home-lab project; not a real procedure.

Revision: B. Owner: Quality Engineering. Applies to: all inner and outer layers inspected on the AOI machines.

## Purpose

Automated optical inspection (AOI) flags every feature that differs from the CAM reference. Many calls are false calls: the feature is acceptable, but dust, oxidation or a reflection made it look different. This instruction explains how to verify calls so that real defects are logged and false calls do not slow the line.

## Verifying each call

1. Open the panel in the verification station software and review every call in order. Never clear a call in bulk.
2. Look at the AOI image and at the real panel under 10x magnification before deciding.
3. Classify the call:
   - Real defect: open, short, nick deeper than 20% of the trace width, protrusion, missing pad, or misregistration. Log it in the MES with station "AOI" and the defect type, and mark the location with a red dot sticker.
   - False call: dust, fingerprint, oxidation, reflection on the panel edge, or a feature inside the allowed tolerance. Mark it "FC" in the verification software.
4. Route panels with real defects to repair or scrap according to the defect limits for the customer class.

## False-call rate limits

- Target: on average fewer than 15 false calls per panel.
- If a panel has more than 30 false calls on 3 panels in a row, stop the AOI machine and call the AOI technician. Typical causes are dirty optics, wrong lighting recipe, or an outdated CAM reference.
- Operators must not change AOI thresholds. Only the AOI technician may change them, with QA approval, and every change is written in the AOI change log.

## When to escalate

If the same real defect appears at the same location on 3 or more panels of one lot, notify the imaging or etching process engineer immediately. Repeated defects at one location usually mean a phototool, artwork or exposure problem, not a random defect.
