# Co-Cube

**A small cube designed to make overwhelming environments easier to navigate.**

Calm Cube is a sensory-support device designed primarily for autistic children who may find it difficult to communicate when their surroundings become overwhelming. It monitors environmental noise levels and provides simple visual feedback through responsive LED lighting, guided breathing exercises, and a way to ask for help.

### How It Works

- **Noise Monitoring:** A USB microphone measures environmental loudness without recording or storing conversations.
- **Responsive Lighting:** RGB LEDs transition between green, orange, and red as noise levels increase.
- **Guided Breathing:** A short button press starts a one-minute breathing exercise, with the lights gradually brightening and fading to guide breathing.
- **Break Requests:** Holding the button for two seconds sends an "I need a break" request to a connected parent or teacher dashboard.
- **AI-Powered Calming Tips:** When high-noise events are detected, Google Gemini generates calming suggestions using numerical summaries rather than audio recordings.
- **Live Dashboard:** A web dashboard displays noise events, activity history, and calming suggestions.

### Modes

- **Kid Mode:** Soft, predictable lighting with no flashing and automatic breathing support when noise remains high.
- **Focus Mode:** More sensitive noise monitoring.
- **Gamer Mode:** A playful noise-responsive rage meter with flashing lighting effects.

### Technology Stack

- **Hardware:** Raspberry Pi 5, RGB LEDs, USB microphone, push button
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
