# Co-Cube

**A small cube designed to make overwhelming environments easier to navigate.**

Calm Cube is a sensory-support device designed primarily for autistic children who may find it difficult to communicate when their surroundings become overwhelming. It monitors environmental noise levels and provides simple visual feedback through responsive LED lighting, guided breathing exercises, and a way to ask for help.

### How It Works

- **Noise Monitoring:** A microphone measures loudness only (never words). QNX's USB audio driver can't record from USB mics yet, so the laptop's mic measures the levels (`laptop_mic.py`) and sends just the numbers to the Pi over Wi-Fi.
- **Responsive Lighting:** Red, green and yellow LEDs mix green → yellow → orange → red as noise levels increase.
- **Guided Breathing:** A short button press starts a one-minute breathing exercise, with the lights gradually brightening and fading to guide breathing.
- **Break Requests:** Holding the button for two seconds sends an "I need a break" request to a connected parent or teacher dashboard.
- **AI-Powered Calming Tips:** When high-noise events are detected, Google Gemini generates calming suggestions using numerical summaries rather than audio recordings.
- **Live Dashboard:** A web dashboard displays noise events, activity history, and calming suggestions.

### Modes

- **Kid Mode:** Soft, predictable lighting with no flashing and automatic breathing support when noise remains high.
- **Focus Mode:** More sensitive noise monitoring.
- **Gamer Mode:** A playful noise-responsive rage meter with flashing lighting effects.

### Technology Stack

- **Hardware:** Raspberry Pi 5, red/green/yellow LEDs (200 Ω), push button, speaker, laptop mic over Wi-Fi
- **Operating System:** QNX
- **Programming:** Python
- **Frontend:** HTML, CSS, JavaScript
- **AI:** Google Gemini API
- **Physical Design:** 3D-printed enclosure using Bambu Lab P1S

### Privacy by Design

Calm Cube is designed to process sound levels rather than conversations. It does not need to record or store audio. AI-generated suggestions are based on numerical activity summaries, not microphone recordings.

### Our Goal

We want to explore how accessible technology can help people communicate their needs and interact with their environments in ways that work better for them.

**Built at Hack Western 2026.**

### Run it

Easiest: python start_cube.py on the laptop (one window: mic, speaker, and starts the cube on the Pi; Pi password qnxuser).

```
# laptop (same Wi-Fi as the Pi)
python laptop_mic.py 172.23.42.90
python laptop_speaker.py            # only if the speaker is connected to the laptop

# Pi (QNX)
python3 calm_cube.py --net --mode kid                          # speaker on the Pi
python3 calm_cube.py --net --mode kid --speaker-laptop LAPTOP_IP   # speaker on the laptop
```
Dashboard: http://172.23.42.90:8080
