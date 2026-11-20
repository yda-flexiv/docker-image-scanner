# Build Image Retention

Which build images the Flexiv software team needs to keep for retained revisions or for an active release.

## Language

**Required Image**:
An image in the target registry repository referenced by a retained revision. A requirement does not imply the image has been run recently.
_Avoid_: In-use image, safe-to-keep image

**Retained Release Branch**:
A release branch within the supported version range. Its tip is covered by the retention policy regardless of age.

**Active Release**:
An optional release line explicitly selected for broader branch retention. An image is not required merely because its tag matches the release version.

**Release-Derived Branch**:
A branch based on the active release line, whether identified from its history or explicitly designated. Its tip is covered by the retention policy regardless of age.

**Integration Branch**:
A long-lived development branch whose tip and recent mainline revisions are covered by the retention policy. `develop` and `v4.x_develop` are integration branches.

**Active Feature Branch**:
A feature branch whose tip is within the feature recency window. Its tip, not its earlier revisions, is covered by the retention policy.

**Unclassified Branch**:
A branch not matched by a branch rule. Its tip is covered by the retention policy pending review.
