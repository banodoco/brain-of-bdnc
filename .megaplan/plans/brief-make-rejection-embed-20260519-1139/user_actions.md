# User Actions

## After Execute

- **U1**: After deploy, manually verify in the production Discord guild that: (a) a structured topic posted in #ltx_resources with same-channel citations renders citations as clickable `<URL>` plain-text links, not collapsed `#channel-name` pills; (b) a rejection line in the admin-embed trace channel shows a clickable `jump:` URL and `media_url:` line. This is a visual Discord client-side rendering check that automated tests cannot cover.
  Rationale: Discord client-side rendering of self-referential message URLs and angle-bracket link suppression cannot be verified from automated tests — confirmation requires visually posting a structured topic in #ltx_resources after deploy and checking that citations render as clickable links, not as collapsed channel pills.
