🚦 Smart Green Corridor v2

📌 Overview

Smart Green Corridor v2 is an enhanced simulation system for intelligent traffic management that prioritizes emergency vehicles by dynamically creating a green corridor using real-time traffic control logic.

This version improves modularity, structure, and simulation efficiency over previous implementations.


---

🏆 Achievement

🥈 1st Runner-Up at DIPEX 2026
Recognized for innovation in smart traffic systems and emergency response optimization.


---

🚀 Features

🚑 Ambulance Priority System

Detects emergency vehicle routes

Clears traffic dynamically


🚦 Adaptive Traffic Signals

Adjusts signal timing based on congestion


🧠 Improved Simulation Logic

Optimized backend scripts

Cleaner modular structure


📊 Traffic Data Handling

Uses SUMO datasets for realistic simulation




---

🏗️ Project Structure

smartGreenCorridor_v2/
│── frontend/                # UI / visualization (if used)
│── scripts/                 # Core simulation scripts
│── sumo_data/               # SUMO configuration & network files
│
│── backend_ambulance_web.py # Main backend logic
│── README.md


---

⚙️ Working Principle

1. SUMO simulates traffic conditions


2. Backend processes traffic density


3. When ambulance is detected:

Route is identified

Signals turn GREEN along the path



4. After passage:

System restores normal traffic flow





---

🧠 Logic Flow

IF ambulance detected:
    Identify shortest path
    Override signals to GREEN
    Stop cross traffic
ELSE:
    Run adaptive traffic control


---

🛠️ Tech Stack

Simulation: SUMO (Simulation of Urban Mobility)

Programming: Python

Interface: TraCI

Frontend: JavaScript (optional UI)



---

▶️ Setup & Installation

1️⃣ Clone Repository

git clone https://github.com/your-username/smartGreenCorridor_v2.git
cd smartGreenCorridor_v2

2️⃣ Install Requirements

pip install -r requirements.txt

3️⃣ Install SUMO

Download: https://www.eclipse.org/sumo/

Set environment variable:

export SUMO_HOME=/path/to/sumo

4️⃣ Run Project

python backend_ambulance_web.py


---

📊 Data Flow

SUMO → Python Backend → Signal Control → Simulation Output


---

🌟 Advantages

Faster emergency response 🚑

Reduced traffic congestion 🚗

Scalable smart city solution 🌆

Clean modular architecture



---

🔮 Future Enhancements

🤖 AI-based traffic prediction

📍 Live GPS ambulance tracking

☁️ Cloud dashboard

📡 Real-time traffic APIs



---

👨‍💻 Authors

Gauravi Naik

Rahul Yadav

Krishna Bitthariya

Varun Mudaliyar
