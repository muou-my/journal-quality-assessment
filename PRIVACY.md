# Privacy and third-party transfer / 数据隐私

This distribution contains software and synthetic fixtures only. It is not a hosted
service and has no analytics, telemetry upload or shared research database.
The user controls their local CSV, metadata mapping, SQLite stores, reports and keys.
OS file permissions are the isolation boundary; do not expose these directories as
a multi-tenant service without additional access controls.

API mode sends the selected two anonymous IDs, titles, abstracts, task/run routing
and complete assessment instructions to api.deepseek.com over HTTPS. It does not
include local DOI/journal mapping columns, authors or affiliations as separate
fields. Titles/abstracts may themselves identify authors, journals or other personal
information; anonymity is not guaranteed. Agent-file mode also discloses content
to the selected Agent environment under that service's own terms.

Before paid execution the CLI displays cost estimates and transmitted fields,
requiring explicit consent. Users must have appropriate rights/permissions to
process and send article text. Review DeepSeek's current privacy/retention policies
and your institutional requirements; this package cannot promise third-party
retention, training exclusions, data residency or deletion on the provider's behalf.

Keys are entered locally without echo or read from an ignored local file, not
submitted in chat. Plaintext local credential files require user access protection.
Original responses, evidence and requests remain on disk for scientific audit;
they may contain sensitive content. Export deliberately copies raw responses for
traceability: review/redact your own exports before sharing. Removing a local run
does not request deletion from a third-party provider or backups.

No public website, support contact, retention contract or verified publisher has
been invented. A directory publisher must provide their own accurate contact and
privacy information before submission, and adapt this notice if hosting services.
