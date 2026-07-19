"""Version anchors for behavior that changes evaluation outcomes."""

from __future__ import annotations


# Version 2 aligns final-response grading with the production public event
# projection and requires post-reassembly trace redaction. Historical reports
# without this field are treated as contract version 1.
EVALUATION_CONTRACT_VERSION = 2
