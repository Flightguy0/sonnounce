# Sonnounce for Home Assistant

Send voice announcements to your Sonos speakers from the Sonnounce app – also when you are away from home.

The Sonnounce app (iPhone, iPad, Mac and Android) talks directly to your Sonos speakers when you are on the same Wi-Fi. This integration lets Home Assistant pass announcements on to the speakers when you are not at home. The recording goes from the app to your own Home Assistant, which plays it on the speakers. It is not stored anywhere on the internet.

## What it does

- Receives announcements from the Sonnounce app and plays them on your Sonos speakers, over the music, which is turned down while the announcement plays.
- Works with S2 and S1 speakers. On speakers that cannot play an announcement over the music, the music pauses and continues afterwards.
- Can save every announcement sent in the house ("Save announcements"), until you download them from the app.
- Fires events (`sonnounce_played`, `sonnounce_failed`, `sonnounce_speakers_lost`, `sonnounce_speakers_found`) you can use in automations.

## Installation

1. In HACS, search for **Sonnounce** and download it.
   (If it does not show up yet, add `https://github.com/Flightguy0/sonnounce` in HACS as a custom repository, category Integration.)
2. Restart Home Assistant.
3. Home Assistant finds your Sonos speakers and offers to set up Sonnounce. Confirm. You can also add it under Settings › Devices & services › Add integration › Sonnounce.
4. Under Settings › Devices & services › Sonnounce, choose **Configure** and then **Add a phone or computer**. This opens pairing for 5 minutes.
5. Within those 5 minutes, open the Sonnounce app at home, on the same Wi-Fi. It finds your Home Assistant and offers to add it. No login is needed in the app.

Repeat steps 4 and 5 for each phone or computer. Pairing closes as soon as one device has been added.

## Away from home

To send announcements when you are out, your Home Assistant must be reachable from the internet – for example with Home Assistant Cloud, or the free Tailscale add-on. The app finds the address by itself.

## Settings

Under Settings › Devices & services › Sonnounce › Configure:

- **Add a phone or computer** – opens pairing for 5 minutes, see above.

Under Configure › Settings:

- **Save announcements**, and for how many days.
- **Address of a speaker** – only needed if Home Assistant and the speakers are on separate networks.
- **Forget all phones and computers** – makes a new key, so every device must be added again at home.

## Privacy

The app gets its key only while a Home Assistant administrator has opened pairing (Configure › Add a phone or computer), and only from your home network – never through Home Assistant Cloud. Pairing closes after 5 minutes or as soon as one device has been added, and Home Assistant shows a notification when a device is added. Every other request must carry that key; too many wrong keys from one address lock out only that address for 15 minutes. Nothing is sent to any service other than your own Home Assistant.

Sonos is a trademark of Sonos, Inc. Sonnounce is not made by, endorsed by or affiliated with Sonos, Inc.
