# Skill catalog and installation management

The Skill market and enterprise settings share one management component and
backend policy. `is_builtin` records origin, not immutability. Platform admins
(including platform Identity administrators) manage catalog entries; tenant
admins manage their own tenant's entries; publishers manage their own entries.
Tenant admins may hide platform entries locally or copy them into an independent
tenant entry with a distinct folder. They never edit another tenant's definition.

`Skill.updated_at` is the catalog modification time shown in cards and details.
Content changes advance `version`; management changes may also advance it.
Republishing an existing entry always advances its version, including drafts,
so a frozen older rollout cannot overwrite a newer installed publication.
Installation activity and tenant hiding do not change the catalog timestamp.
Relisting publishes the current catalog contents; it does not recopy a source
Agent. Publishing from an Agent remains an explicit operation.

Builtin seeding records one import receipt per bundled folder in SystemSetting.
It preserves existing content, status and `is_default`; deletion retains the
receipt so restart cannot resurrect an entry. The initial migration preserves
existing defaults. New bundles after initialization are not default-installed.
The base workspace template no longer installs Skill packages directly.
`is_default` is database configuration applied to new Agents only, subject to
published status and tenant visibility. Defaults never automatically update,
reinstall or uninstall existing Agent copies.

SkillInstall is installation provenance, including new-Agent/default installs.
A one-time adoption only associates complete byte-identical legacy builtin
folders, preserving existing inactive records and independently modified folders.
Unknown local folders are not claimed by matching names.

A publisher/authorized administrator may explicitly update every active
installation of its catalog entry, including voluntary public installations in
other tenants. SkillUpdateJob freezes files/version and target Agent IDs. API
background tasks use the renewable job lease and existing workspace write queue.
Only that Skill directory is replaced, including removal of obsolete files;
local modifications are overwritten after UI confirmation. Other workspace files
and independently copied Skills are untouched. Each target records success,
failure or skip; a failure restores the previous directory. Deleted/uninstalled
Agents and installations already on a newer version are skipped. Completed
results persist; retry resumes failed/pending targets with the same snapshot,
including pending work interrupted by an API process restart. No separate queue
or automatic installation subscription is introduced.

Progress responses expose aggregate counts only, never cross-tenant Agent IDs,
files or provider errors. Definition management authority never grants arbitrary
access to target workspaces. In-flight model context is not rewritten; subsequent
file loads see updated contents. Downlisting/hiding prevents new installation,
while existing copies remain usable. Deleting an offline catalog entry preserves
workspace files. There is no runtime kill switch or automatic merge mechanism.

Schema migration adds tenant hiding and rollout records. Downgrading drops these
new records but does not remove catalog entries or installation files; stop
rollouts before a downgrade. Bundled import receipts remain in existing settings.

Administrators can upload a ZIP Skill package or a single SKILL.md through the
same management surface. Upload creates a draft with explicit tenant/platform
ownership and never overwrites an existing folder. Packages accept UTF-8 text,
normalize one wrapper directory, and reject unsafe paths, duplicate files,
symlinks and oversized contents before persistence. Platform scope requires
platform administration; tenant uploads stay in the administrator's tenant.
Publishing from an Agent and local upload both use the shared drawer UI.
