# CLI Admin Data Management Design

## Goal

Add local CLI commands for listing, reading, and deleting agent runtime data without introducing a backend service.

## Scope

The feature manages the existing local file layout:

- User profile: `memory/users/{user_id}/USER.md`
- Conversation and project memory: `memory/sessions/{user_id}/{project_id}/`
- Project journal: `journals/{user_id}/{project_id}.md`
- Project workspace: `workspaces/{user_id}/{project_id}/`

It does not add HTTP APIs, authentication, database storage, or changes to the chat loop.

## Architecture

Create a small reusable admin module that knows how to inspect runtime directories and safely delete scoped paths. The root CLI adds an `admin` subcommand group and delegates all filesystem work to that module.

Deletion is conservative: commands print the affected paths unless `--yes` is provided, and every path is resolved under the configured runtime roots before removal.

## Commands

- `python agent.py admin list-users`
- `python agent.py admin list-projects --user-id <id>`
- `python agent.py admin show-user --user-id <id>`
- `python agent.py admin show-conversation --user-id <id> --project-id <id>`
- `python agent.py admin show-project --user-id <id> --project-id <id>`
- `python agent.py admin delete-user --user-id <id> --yes`
- `python agent.py admin delete-conversation --user-id <id> --project-id <id> --yes`
- `python agent.py admin delete-project --user-id <id> --project-id <id> --yes`

## Testing

Use temporary runtime roots so tests do not touch real local data. Cover listing, reading, dry-run deletion, confirmed deletion, and CLI command output.
