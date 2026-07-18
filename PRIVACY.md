# Privacy policy

Effective date: 2026-07-16

Achievement Box is self-hosted software. The project does not operate a cloud
service, user-account system, analytics platform or telemetry endpoint, and the
project maintainers do not receive data from installations of the software.

## Data kept on the Achievement Box host

The following data may be stored on the PC or appliance running the daemon:

- RetroAchievements username and password in `daemon/.env`, if the operator
  chooses that configuration method. They remain on the host except when used
  to authenticate directly with RetroAchievements over HTTPS.
- Optional local web-interface username and password in `daemon/.env`. These
  authenticate to the local daemon and are not sent to Achievement Box or
  RetroAchievements. They should not be the same as the RetroAchievements
  password.
- Game-library metadata, ROM paths and filenames, artwork, achievement badges,
  system icons and downloaded metadata archives under `daemon/cache/`.
- The preferred Achievement Box mapper state and media-region setting.
- Achievement and leaderboard submissions created while RetroAchievements is
  unreachable. Exact request payloads, which include the RA account token, are
  authenticated and encrypted under a locally generated key in
  `daemon/cache/ra-offline/`. Identical pending requests are deduplicated and
  removed after delivery or a non-retryable server response.
- If browser notifications are enabled, web-push subscription endpoints and
  public encryption keys in `daemon/cache/push_subs.json`, plus a locally
  generated VAPID keypair in `daemon/cache/vapid.json`.
- Operational output in the terminal and a local native-crash log. Logs may
  contain game titles, RetroAchievements game IDs, Rich Presence text and error
  details, but are not uploaded automatically.

The browser stores display-filter preferences in local storage and may retain
normal browser caches, the service worker and notification permission. The web
interface does not set tracking or advertising cookies.

## Data sent to third parties

Achievement Box contacts third parties only to provide requested features:

- **RetroAchievements** receives account authentication, game identification,
  session activity, Rich Presence, achievement unlocks and related client
  requests. Badge images are also downloaded from RetroAchievements.
- **Browser push providers** receive an optional push notification and its
  destination endpoint. The provider depends on the browser and operating
  system and may be operated by Google, Mozilla, Apple, Microsoft or another
  browser vendor. Push is disabled until a browser user opts in.
- **LaunchBox, libretro and OpenVGDB/GitHub** may receive ordinary download
  requests when metadata or artwork is synchronized or displayed.
- **Google Fonts** receives a request when the web interface loads its hosted
  fonts. **YouTube/Google** receives requests if video thumbnails are displayed
  or a user opens an embedded game video.

These requests normally disclose the host or browser's IP address, user agent,
request time and the requested resource to the relevant provider. Each provider
controls its own processing, retention and server locations under its own
privacy policy. Achievement Box does not add a project-specific identifier or
combine this information into a project-operated profile.

## Local-network visibility and security

The web interface exposes game, achievement and account-display state to
browsers that can reach the daemon. By default it assumes a trusted local
network. Operators who share a network should configure `WEB_PASSWORD` and
HTTPS as described in the project's security guidance. RetroAchievements
credentials are never returned through the browser API.

## Retention and deletion

Achievement Box has no remote retention because it receives no installation
data. Local files remain until the operator removes them. To erase local data:

1. stop the daemon;
2. delete `daemon/.env` to remove configured credentials;
3. delete `daemon/cache/` to remove metadata, artwork, mapper preferences,
   notification subscriptions, VAPID keys and locally cached account/game data;
4. delete any local crash log if present; and
5. clear the site's storage, notification permission and service worker in each
   browser that used the interface.

Dead push subscriptions are also removed after the push provider reports them
as expired. To remove data held by a third party, use that provider's account,
privacy or data-deletion process.

## GDPR and other privacy rights

Because the project maintainers do not receive personal data from running
installations, there is no central Achievement Box dataset against which to
make an access or deletion request. The person or organisation operating a
shared installation controls its local data and is responsible for an
appropriate lawful basis, access control and response to users' rights where
applicable. Third-party services remain responsible for the data they receive.

If a future release introduces project-operated servers, analytics, telemetry
or crash reporting, this policy must be updated before that processing starts.

## Contact and changes

Privacy questions and corrections should be raised through the public
Achievement Box repository's issue tracker. Material changes will be recorded
in the repository history and reflected by a new effective date above.
