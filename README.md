# SKAO Memo Series – Submission Requests (Demo)

> **Note:** This is a proof-of-concept demo. All content is fictitious.

To submit a memo, open a new issue: **Issues → New issue → Memo submission**.

**This repository is public.** Only provide the series and, optionally, a
provisional title. Do not include any content from your memo.

Once an editor approves your request, a private repository will be created
for your memo and you will receive an invitation to it by email. All further
work, including the review, takes place in that private repository.

## For editors

All editorial actions are done with labels on the submission request:

1. **Approve for review:** add `approved-for-review`. A private repository
   is created for the memo and the author is invited to it.
2. **Accept and publish:** merge the author's pull request in the memo
   repository, then add `accepted`. The memo is published on Zenodo and
   copied to the public repository as e.g. SV-2026-001.v1, and the label
   changes to `published`.
3. **Publish a new version:** the author sets `version` in `memo.yaml` to
   the next number (e.g. 1 → 2) and replaces the files in `publish/` in a
   pull request. Merge it, then add `accepted` again. The new version is
   published as e.g. SV-2026-001.v2; if `version` has not been increased,
   nothing is published.
4. **Reject:** add `rejected` and close the request.

If a publication fails, the request receives a comment with a link to the
log and `accepted` is removed. Fix the problem and add `accepted` again.
