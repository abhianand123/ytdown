# Neumorphic (Soft UI) Audio Studio — Skills, Repositories & Design System Guide

This document lists all the skills, CLI tools, external repositories, design frameworks, and token rules that were used to build the hardware-grade Neumorphic (Soft UI) interface and eliminate the generic "AI look".

---

## 1. Core Design Skills & Tooling

### 1.1 `ui-ux-pro-max-cli` & UI/UX Pro Max Skill
* **GitHub Repository**: [nextlevelbuilder/ui-ux-pro-max-skill](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill)
* **npm Package**: [`ui-ux-pro-max-cli`](https://www.npmjs.com/package/ui-ux-pro-max-cli)
* **Installation Command**:
  ```bash
  npm i -g ui-ux-pro-max-cli
  uipro init --ai antigravity
  ```
* **Local Skill Location**: [`.agents/skills/ui-ux-pro-max/`](file:///c:/Users/Abhi%20Anand/Documents/D_Drive_Backup/ytdown/.agents/skills/ui-ux-pro-max/SKILL.md)
* **Search Utility**:
  ```bash
  python .agents/skills/ui-ux-pro-max/scripts/search.py neumorphism
  python .agents/skills/ui-ux-pro-max/scripts/search.py accessibility
  ```
* **Role in Project**:
  - Supplied the mathematical foundation for Style #24 **Neumorphism (Soft UI)**.
  - Provided the dual-shadow extrusion engine (highlight shadow on top-left, absorption shadow on bottom-right).
  - Provided debossed sunken (`inset`) rules for tactile sliders, inputs, radar scopes, and active buttons.
  - Enforced accessibility contrast ratios (WCAG 4.5:1 text-to-background minimum).

---

### 1.2 Impeccable Design & Sloplint Rules
* **GitHub Repository**: [pbakaus/impeccable](https://github.com/pbakaus/impeccable)
* **Author**: Paul Bakaus (Creator of Impeccable)
* **Role in Project**:
  - **Eliminating "AI Slop"**: AI generators habitually default to generic dark slate (`#0f172a`) with electric cyan/purple floating glow effects and blurry glassmorphism that lack physical presence.
  - **Tactile Weight & Border Outlines**: Injected hairline boundary lines (`--nm-border`) so Neumorphic elements maintain structural definition under varying monitor calibrations without "dissolving into mud".
  - **Rule 49 Compliance**: Prevented text wrap glitches on interactive chips and buttons with explicit `white-space: nowrap;`.
  - **Clear Lighting Model**: Fixed 135° directional light source (top-left to bottom-right) across all UI elements so elevations and depressions feel physically consistent.

---

### 1.3 `ui-styling` Skill
* **Local Skill Location**: [`.agents/skills/ui-styling/`](file:///c:/Users/Abhi%20Anand/Documents/D_Drive_Backup/ytdown/.agents/skills/ui-styling/SKILL.md)
* **Role in Project**:
  - Utility-first CSS architecture with Tailwind CSS.
  - Custom CSS property engine enabling seamless, zero-flicker live toggling between Dark Industrial and Soft Clay modes.
  - Custom range sliders, tactile toggles, and responsive layouts across mobile, tablet, and desktop viewports.

---

### 1.4 `design-system` Skill
* **Local Skill Location**: [`.agents/skills/design-system/`](file:///c:/Users/Abhi%20Anand/Documents/D_Drive_Backup/ytdown/.agents/skills/design-system/SKILL.md)
* **Role in Project**:
  - **Three-Layer Token Architecture**:
    1. **Primitive Tokens**: Pure hex colors and elevation lengths.
    2. **Semantic Tokens**: `--nm-surface`, `--nm-raised`, `--nm-inset`, `--color-accent`.
    3. **Component Tokens**: `.nm-card`, `.nm-btn`, `.nm-azimuth-scope`, `.nm-toggle-track`.
  - Consistent component contracts across both `templates/index.html` and `templates/8d.html`.

---

### 1.5 `design` & `brand` Skills
* **Local Skill Locations**:
  - [`.agents/skills/design/`](file:///c:/Users/Abhi%20Anand/Documents/D_Drive_Backup/ytdown/.agents/skills/design/SKILL.md)
  - [`.agents/skills/brand/`](file:///c:/Users/Abhi%20Anand/Documents/D_Drive_Backup/ytdown/.agents/skills/brand/SKILL.md)
* **Role in Project**:
  - Typography pairings: **Inter** (clean, geometric sans-serif for UI labels) paired with **JetBrains Mono** (technical precision monospace for numerical readouts, timestamps, Hz, and decibels).
  - Audio studio branding inspired by legendary industrial hardware design.

---

## 2. Real-World Design Benchmarks & Palettes

To escape the generic "AI generator look", we researched authentic human-designed hardware and top-rated Dribbble benchmarks:

### Palette 1: Dark Industrial Obsidian & Signal Orange (Default Theme)
* **Inspiration**: **Teenage Engineering** (OP-1, OP-Z, TX-6) hardware master desks and vintage Apollo avionics consoles.
* **Canvas Background**: `#151517` (Deep matte obsidian)
* **Tactile Cards / Surfaces**: `#1e1e22`
* **Light Shadow (Specular Rim)**: `rgba(255, 255, 255, 0.05)`
* **Dark Shadow (Ambient Absorption)**: `rgba(0, 0, 0, 0.65)`
* **Accent Color**: `#f05a24` (Industrial Signal Orange)
* **Text Color**: `#f5f5f4` (Warm Alabaster, >13:1 contrast ratio)

### Palette 2: Classic Soft Clay & Terracotta (Light Theme)
* **Inspiration**: **Alexander Plyuto (`alexplyuto`)** — the seminal 2019/2020 Dribbble work that originated the Neumorphic Soft UI design movement ([Dribbble: Skeuomorph Mobile Banking](https://dribbble.com/shots/8287754-Skeuomorph-Mobile-Banking)).
* **Canvas Background**: `#e0e5ec` (Soft tactile clay)
* **Tactile Cards / Surfaces**: `#e0e5ec`
* **Light Shadow (Diffuse Highlight)**: `#ffffff`
* **Dark Shadow (Soft Ambient Shadow)**: `#a3b1c6`
* **Accent Color**: `#ea580c` (Warm Terracotta)
* **Text Color**: `#1e293b` (Deep Slate, >10:1 contrast ratio)

---

## 3. Neumorphic Shadow Mechanics Code Reference

```css
/* Raised Extrusion (Pushed Out / Standing Proud) */
--nm-raised: -6px -6px 14px var(--nm-shadow-light), 
              6px  6px 14px var(--nm-shadow-dark);

/* Subtle Raised Surface */
--nm-raised-sm: -3px -3px 8px var(--nm-shadow-light), 
                 3px  3px 8px var(--nm-shadow-dark);

/* Sunken / Debossed Inset (Sliders, Inputs, Radar Wells, Pressed Buttons) */
--nm-inset: inset  4px  4px 8px var(--nm-shadow-dark), 
            inset -4px -4px 8px var(--nm-shadow-light);

/* Shallow Inset */
--nm-inset-sm: inset  2px  2px 5px var(--nm-shadow-dark), 
               inset -2px -2px 5px var(--nm-shadow-light);
```

---

## 4. Audio DSP & FFmpeg Filterchain Reference

Alongside the Neumorphic visual redesign, the audio processing backend was rebuilt using these FFmpeg acoustic filters:

| Feature | Filter Chain | Description |
| :--- | :--- | :--- |
| **8D Binaural Orbit** | `stereowiden=delay=18:feedback=0.25:crossfeed=0.25:drymix=0.8,apulsator=mode=sine:hz=0.125:amount=0.85:offset_l=0:offset_r=0.5` | Smooth 360° circular horizontal rotation with custom typed rotation period in seconds. |
| **16D Multi-Axis Orbit** | `stereowiden=delay=25:feedback=0.38:crossfeed=0.32:drymix=0.75,apulsator=mode=sine:hz=0.187:amount=0.85:offset_l=0:offset_r=0.5` | Dual-harmonic trajectory with elevation crossfeed and cathedral reverb. |
| **Vocal Remover (Karaoke)** | `pan=stereo\|c0=c0-c1\|c1=c1-c0,bass=g=3.5:f=120` | Phase cancellation of center-panned lead vocals & lyrics with low-end recovery. |
| **Vocal Isolator (Acapella)** | `highpass=f=200,lowpass=f=4200` | Formant bandpass isolating vocal presence while suppressing side instruments. |
| **Bass Remover** | `highpass=f=250,bass=g=-24:f=180` | High-pass 240Hz filter taking out all basslines, 808s, and low-end rumble. |
| **Sub-Bass Focus** | `lowpass=f=220,bass=g=5:f=100` | Isolates the kick groove and sub-bass frequencies. |
| **Pitch Shifter** | `rubberband=pitch=2^(semitones/12)` | Pristine ±6 semitone pitch shifting without altering playback tempo. |
| **Air Clarity Sheen** | `treble=g=3.5:f=8000,crystalizer=i=1.5` | Harmonic exciter adding spatial presence and top-end shimmer. |
| **Slowed + Reverb** | `atempo=0.88,asetrate=44100*0.92,aresample=44100,aecho=0.8:0.85:80\|1200:0.35\|0.18` | Viral Lo-Fi / Vaporwave slowed aesthetic with ethereal decay. |
| **Nightcore** | `asetrate=44100*1.25,aresample=44100` | High-energy 1.25x tempo speedup with naturally pitched vocals. |
| **432 Hz Tuning** | `asetrate=44100*(432/440),atempo=440/432,aresample=44100` | Retunes standard 440Hz concert pitch to the 432Hz sacred miracle frequency. |

---

## 5. Live Application Access

* **Permanent Gateway**: [https://abhianand123.github.io/ytdown/](https://abhianand123.github.io/ytdown/)
* **Live Studio Direct**: [https://uni-winds-driving-stream.trycloudflare.com/8d](https://uni-winds-driving-stream.trycloudflare.com/8d)
* **Main Downloader**: [https://uni-winds-driving-stream.trycloudflare.com/](https://uni-winds-driving-stream.trycloudflare.com/)
