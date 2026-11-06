# Sonnounce for Home Assistant

Send voice announcements to your Sonos speakers from the Sonnounce app – also when you are away from home.

The Sonnounce app (iPhone, iPad, Mac and Android) talks directly to your Sonos speakers when you are on the same Wi-Fi. This integration lets Home Assistant pass announcements on to the speakers when you are not at home. The recording goes from the app to your own Home Assistant, which plays it on the speakers. It is not stored anywhere on the internet.

## What it does

- Receives announcements from the Sonnounce app and plays them on your Sonos speakers, over the music, which is turned down while the announcement plays.
- Works with S2 and S1 speakers. On speakers that cannot play an announcement over the music, the music pauses and continues afterwards.
- Can save every announcement sent in the house ("Save announcements"), until you download them from the app.
- Fires events (`sonnounce_played`, `sonnounce_failed`, `sonnounce_speakers_lost`, `sonnounce_speakers_found`) you can use in automations.

## Installation

1. Add this repository in Home Assistant's Marketplace (or HACS) as a custom repository, category Integration, and download Sonnounce.
2. Restart Home Assistant.
3. Home Assistant finds your Sonos speakers and offers to set up Sonnounce. Confirm. You can also add it under Settings › Devices & services › Add integration › Sonnounce.
4. Open the Sonnounce app at home, on the same Wi-Fi. It finds your Home Assistant and offers to add it. No login is needed.

## Away from home

To send announcements when you are out, your Home Assistant must be reachable from the internet – for example with Home Assistant Cloud, or the free Tailscale add-on. The app finds the address by itself.

## Settings

Under Settings › Devices & services › Sonnounce › Configure:

- **Save announcements**, and for how many days.
- **Address of a speaker** – only needed if Home Assistant and the speakers are on separate networks.
- **Forget all phones and computers** – makes a new key, so every device must be added again at home.

## Privacy

Only devices on your home network can get the key the app uses. Requests from the internet must carry that key. Nothing is sent to any service other than your own Home Assistant.

Sonos is a trademark of Sonos, Inc. Sonnounce is not made by, endorsed by or affiliated with Sonos, Inc.
