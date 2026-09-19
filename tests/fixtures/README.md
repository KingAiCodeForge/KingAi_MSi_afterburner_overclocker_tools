# Synthetic fixtures

These files contain no captured hardware profile.

`{{SYNTHETIC_VF_CURVE}}` is replaced in each temporary test copy with a
deterministically generated `0x20000` curve containing:

- a 12-byte version/count/reserved header;
- three active points stored as voltage/base-frequency/offset float triplets;
- effective frequencies computed as base plus offset;
- all 256 point slots, with unused slots zeroed;
- a small synthetic footer.

Tests never substitute the placeholder in the tracked fixture or access an
installed MSI Afterburner directory.
