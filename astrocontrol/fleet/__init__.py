"""The fleet dashboard: every Starfront PC on the observatory LAN, on one page.

Personal use, run on any one PC on the LAN (reached over the VPN):

    python -m astrocontrol.fleet            # or:  Starfront.exe --fleet

It holds nothing that matters. Each node (a Starfront PC) is polled on its own
thread with a timeout, so one dead or slow PC never holds up the page; the
cards are rebuilt from the last answer each node gave; commands are forwarded
to the node that owns the telescope, and their progress is read back from
that node's own background-command status (0.2.50). See FLEET-0.2.52.md.
"""
