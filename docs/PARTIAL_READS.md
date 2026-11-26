# Snapshot device read failures (Remote Script 1.41.0)

Successful device/chain snapshots retain their existing fields. Failed requested
parameter reads add parameters_read_status (partial or unavailable) and
parameter_read_errors (original parameter index, or null for enumeration,
plus error text). Successfully observed parameter rows are retained; an empty
list accompanied by an error is not proof the device has no parameters.

Requested rack walks add chains_read_status and chain_read_error when chain
enumeration fails. A chain whose device enumeration fails adds
devices_read_status and device_read_error, retaining observed devices.
Unreadable cheap chain_count is null, not zero. Unreadable automation
inspection produces null automation_enabled, not false.

These additive error indicators are not a native qualification certificate.
Absent indicators from older scripts do not establish complete reads. This
increment does not change host normalization of old data or public capability
names. Optional parameter attributes, chain mixer fields, top-level track/device
collection completeness, and direct rack-reader paths still need separate
qualification/repair. Note enumeration completeness remains a separate scope.
