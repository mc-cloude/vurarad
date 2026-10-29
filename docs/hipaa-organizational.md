**Until the Google Cloud BAA and the AWS BAA are both signed and accepted, none of this architecture is HIPAA-compliant — regardless of how complete the technical controls in `docs/hipaa-controls.md` are.** The BAAs are a prerequisite, not a finishing touch: without them there is no contractual obligation on either cloud to safeguard ePHI, and every control below is operating outside a compliance perimeter.

# HIPAA — organizational obligations code cannot satisfy

`docs/hipaa-controls.md` covers what the code and infrastructure deliver. This
document covers what the code **cannot** deliver — the administrative and
physical obligations that fall to the covered entity / business associate
operating vuraRAD. Shipping §5.1–§5.8 and calling the product "HIPAA compliant"
would be false; these are the user's obligations, restated from the plan
(§5.9) so they are not lost between the design and the launch.

| # | Obligation | Citation | Note |
|---|---|---|---|
| 1 | **Sign the Google Cloud BAA** | §164.308(b)(1) | Free, but must be actively accepted in Cloud Console. Until signed, **none** of the GCP-resident architecture is compliant regardless of code quality |
| 1a | **Sign the AWS BAA** | §164.308(b)(1) | Free, covers S3 + CloudFront where the SPA is hosted (D2). AWS holds no PHI today, but a study id leaking into a URL path on a host with no BAA was the original review finding #11 — the BAA is the belt to that braces |
| 2 | **Documented risk analysis** | §164.308(a)(1)(ii)(A) | Periodic, written, covering this architecture |
| 3 | **Risk management plan** | §164.308(a)(1)(ii)(B) | Remediation tracking |
| 4 | **Designated Security Official** | §164.308(a)(2) | A named person |
| 5 | **Workforce security awareness training** | §164.308(a)(5) | With completion records |
| 6 | **Sanction policy** | §164.308(a)(1)(ii)(C) | For workforce violations |
| 7 | **Breach notification procedures** | §164.404–410 | Written runbook with the 60-day clock |
| 8 | **Contingency / disaster recovery plan** | §164.308(a)(7) | Backup, restore, emergency-mode operation. The tested restore path is in `docs/runbooks/restore.md`; Firestore PITR is 7 days in-place and is **not** a DR plan |
| 9 | **Written policies retained 6 years** | §164.316 | These docs are themselves in scope and are retained alongside the bucket-locked audit trail |
| 10 | **BAAs with any downstream subcontractor** | §164.308(b) | Anyone touching PHI. Today that is Google Cloud (PHI) and AWS (no PHI, but covered). Any future processor added requires a BAA before it receives ePHI |
| 11 | **Physical safeguards for viewing workstations** | §164.310 | Screen locks, positioning, device encryption — the thin-client viewer is only as private as the workstation it runs on |
| 12 | **Accounting of disclosures on request** | §164.528 | The `audit_logs(patientRef, timestamp)` index (§4.5) makes this *technically possible*; the process to respond within 60 days is organizational |
| 13 | **Minimum necessary determination** | §164.502(b) | The RBAC model implements a *proposed* minimum-necessary access policy; the clinical decision about who needs what access is the covered entity's |

The code makes compliance **achievable**. It does not make the organization
**compliant**. Items 1 and 1a are blocking: until both BAAs are accepted, the
technical control matrix is evidence of intent, not of compliance.
