# RDC-53 network survey (Phase 0) - TEMPLATE

Fill in on site. Do **not** change any PLC/HMI address, do not connect the
machine subnet to the Internet, do not run active scanners against the PLC.

## Physical path

```
Main MikroTik/SFP ── fibre ── media converter ── unmanaged switch (machine)
                                                     ├── PLC / existing machine Ethernet
                                                     └── service laptop
```

| Item | Value |
|---|---|
| Fibre / SFP module type | |
| Media converter model | |
| Switch model / port | |
| Date / performed by | |

## Devices

| Device | Model | IP | Subnet / GW | MAC | Ping allowed | Notes |
|---|---|---|---|---|---|---|
| PLC CPU | | | | | | |
| HMI | | | | | | |
| Other | | | | | | |

How each value was obtained (GX Works3 connection settings, HMI system
screen, label on device, passive ARP observation, ...):

## Checks

- [ ] Fibre and media converter connected, link up both ends
- [ ] Service laptop on the machine switch, address chosen outside existing device IPs
- [ ] GX Works3 connects over the new path exactly as before
- [ ] Machine operation unchanged during and after the test (operator confirmation)
- [ ] No PLC/HMI address or parameter changed
- [ ] Machine subnet not reachable from the Internet
- [ ] SLMP/MC protocol port open on the PLC? (read from PLC parameters in GX Works3, do not change)

## Exit criterion

A laptop can communicate with the PLC over the new fibre path without any
change in machine operation.

Then set `RDC53_PLC_IP` in `deploy/config.env` (and `deploy/docker/.env` on
the VM). The machine stays `enabled: false` until M4.
