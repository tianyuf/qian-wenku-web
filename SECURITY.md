# Security Policy

## Reporting

Please report security vulnerabilities privately to the repository maintainers rather than opening a public issue. Include affected versions, reproduction steps, impact, and any suggested mitigation. Maintainers will acknowledge a report and coordinate disclosure after a fix is available.

Do not include production corpus data, credentials, private paths, or personal information in a report.

## Scope

The application is designed for read-only prepared artifacts. A deployment should run the preflight check, keep the artifact and application tree non-writable by the service, terminate TLS at nginx, and avoid exposing Gunicorn directly. Data licensing and content corrections are outside the code security policy.
