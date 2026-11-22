# Security policy

## Reporting

Email **security@adeu.ai** to report a suspected vulnerability privately.

Include the affected package or desktop bundle and its version, the operating system and MCP host, the tool call or command, and what happened. Show the issue on a released version. Remove credentials, customer documents and other confidential material from anything you send.

## File access model

Adeu has no project boundary by default. The tools read and save Word documents at any path the MCP host or agent supplies, in any folder the server's account can access, including network shares. These controls apply in every configuration:

- An existing file is replaced only if it is empty or a Word document.
- Outputs use `.docx` or the input's own ending, and a PDF export uses `.pdf`. Macro-enabled and template endings (`.docm`, `.dotm`, `.dotx`, `.dot`) are written only beside the input.
- A file is opened in its desktop application only if it is a Word document.

Operators who need a boundary set `ADEU_ALLOWED_DIRS`, which limits every read, write and open to the listed folders. See [File access](README.md#file-access).

## Scope

In scope: a way around one of the controls above, or around `ADEU_ALLOWED_DIRS`, shown on a released version.

Not in scope:

- The default described above, including reads, writes and network paths when `ADEU_ALLOWED_DIRS` is unset.
- Error messages and resolved paths returned when `ADEU_ALLOWED_DIRS` is unset.
- Issues that require control of the MCP host, the server's environment or its launch configuration.
- Scanner or model output without a demonstration on a released version.

Reports outside this scope receive no further reply.

## Fixes and advisories

Fixes go into the latest release. When the fixed version is released, Adeu publishes an advisory with the affected and fixed versions in [Adeu security advisories](https://github.com/dealfluence/adeu/security/advisories). The maintainers decide release timing; deadlines set by a reporter do not change it.

Advisories record the issue and the versions. They carry no reporter credits, links or company names.
