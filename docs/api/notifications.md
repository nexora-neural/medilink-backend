# In-app notifications

The authenticated account can read only its own notifications. Both routes use
the shared Firebase identity and authenticated request limit, return private
responses with `Cache-Control: no-store`, and expose the notification fields in
the reference API contract.

## List notifications

`GET /api/v1/me/notifications`

Query parameters:

- `page`: starts at 1; default 1.
- `page_size`: 1–100; default 20.
- `unread`: optional boolean. `true` returns unread notifications and `false`
  returns read notifications.

Results are ordered by `created_at DESC, id DESC`. The response is a
`NotificationsReadPage` with `items`, `page`, `page_size`, and `total`. Unknown
query parameters are rejected. Notification content and related identifiers
are returned only to the account that owns the notification.

## Mark a notification read

`POST /api/v1/notifications/{notification_id}/read`

There is no request body. The response is the caller's `NotificationsRead`
record. A notification owned by another account is concealed with the same
404 response as a missing notification. Repeating the request preserves the
original `read_at` timestamp. The first state change appends a
`notification.read` audit event in the same database transaction.

## Notification preferences

Preferences use the existing patient profile contract rather than a new
notifications endpoint. `GET /api/v1/me` returns the authenticated patient's
`notification_preferences`; `PATCH /api/v1/me/patient-profile` accepts the
`medication_in_app` and `vaccination_in_app` boolean fields. Updates are
patient-owned and audited by the profile route.

`medication_in_app: false` suppresses new in-app dose reminder rows. It does
not delete existing notifications or alter schedules, occurrences, dose
statuses, or adherence records. When a preference has never been set, the
worker keeps the existing enabled behavior. `vaccination_in_app` remains
stored through the profile contract; this backend base does not yet contain a
vaccine reminder producer. In-app notifications do not promise delivery while
a browser is closed or device push delivery.
