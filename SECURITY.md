# Security

This project reads OpenReview data with the permissions of the configured
account and sends notification content through the configured Bark service.
It is not affiliated with OpenReview, ICLR, Bark or Railway.

## Reporting

Never include passwords, session tokens, device tokens, Bark keys, local
configuration files or review databases in a public issue. If private security
reporting is enabled on this repository, use GitHub's **Security → Report a
vulnerability** flow. Otherwise, open an issue that describes the impact without
credentials or private data so a private reporting channel can be arranged.

## Credential handling

- Windows interactive login stores its session using current-user DPAPI;
  passwords and one-time codes are not persisted by that flow.
- Environment variables, manual legacy configuration and `cloud-config`
  exports can contain plaintext secrets. Keep them in a local protected
  directory or your deployment provider's secret settings.
- `.gitignore` and `.dockerignore` exclude private working state. Ignore rules
  do not make a previously committed credential safe: revoke an exposed token
  or key and remove it from repository history.
- GitHub issues and CI logs are not a credential storage channel.
- With the default Bark endpoint, notification content is sent to Bark's public
  service and Apple APNs; this project does not enable Bark message encryption.
  Decide whether this is appropriate for the review content you can access.

## Operational limits

Use a single worker per state database. Retain the persistent volume across
restarts. Sessions can expire or be revoked, and sites can require manual access
verification. Handle those conditions through normal authentication or official
verification workflows; do not disable TLS checks or bypass access controls.
