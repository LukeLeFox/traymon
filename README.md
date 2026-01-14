# TrayMon (Tray Performance Monitor) - Vibe Coded

Piccola tray app per Windows che mostra statistiche di sistema (CPU/RAM/NET/DISK + temperature e GPU via LibreHardwareMonitor) e un overlay flottante configurabile.

> Nota: la funzione "click-through" dell’overlay non è stata implementata perché con Tkinter su Windows 11 24H2 risulta non affidabile in modo consistente.

---

## Funzionalità

- **Tray icon** (system tray) con tooltip multi-riga aggiornato in realtime
- **Overlay flottante** opzionale (always-on-top)
  - ON/OFF dal menu
  - **Posizioni predefinite** (griglia): top-left, top-right, bottom-left, bottom-right, center
  - **Coordinate manuali** (X/Y)
  - **Spostamento a mano**: sblocca/blocca (drag con mouse)
  - **Salvataggio posizione** automatico:
    - quando rilasci il mouse (fine drag)
    - quando riblocchi l’overlay
  - **Colori** overlay (preset + custom) salvati su `config.json`
- **Selezione NIC**
  - singola interfaccia (`net_iface`)
  - multi-interfaccia (`net_ifaces`) con modalità:
    - `aggregate` (somma traffico)
    - `separate` (una riga per NIC)
- **LibreHardwareMonitor (LHM)**
  - avvio automatico dell’eseguibile `LibreHardwareMonitor.exe` (se presente)
  - lettura sensori via WMI (`root\LibreHardwareMonitor`)
  - stop automatico LHM in uscita dalla tray app
- **Ricarica config.json** dal menu (senza riavviare l’app)

---

## Dipendenze (Python)

- `psutil`
- `pystray`
- `Pillow`
- `wmi` *(solo per temperature/GPU via LHM; se non installato LHM viene ignorato)*

Install (consigliato in venv):

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Esempio `requirements.txt`:

```txt
psutil
pystray
Pillow
wmi
```

> Nota: `wmi` funziona su Windows (non su WSL/Linux).  
> Se sviluppi su Linux, testa runtime su Windows.

---

## Struttura cartelle attesa

Minimo:

```text
TrayMon/
├─ TrayMon.py
├─ config.json
```

Con temperature/GPU:

```text
TrayMon/
├─ TrayMon.py
├─ config.json
└─ LibreHardwareMonitor.exe
```

---

## config.json (tutte le opzioni)

Il parser supporta anche commenti `#` e `//` su singola riga.

### Refresh / Tooltip
- `refresh_s` (float)  
  Intervallo di refresh in secondi (min interno 0.25s nel loop).
- `tooltip_lines` (int)  
  Quante righe massimo mostrare nel tooltip della tray.

### Selettori “mostra / non mostra”
- `show_cpu` (bool)
- `show_ram` (bool)
- `show_net` (bool)
- `show_disk` (bool)
- `show_temps` (bool)  
  Master switch per temperature (richiede LHM + WMI).
- `show_cpu_temp` (bool)  
  Abilita la temp CPU (se `show_temps=true`).
- `show_gpu` (bool)  
  Abilita GPU (se `show_temps=true` e LHM disponibile).
- `show_gpu_load` (bool)
- `show_gpu_temp` (bool)

### Rete (NIC)
**Modalità singola:**
- `net_iface` (string)  
  - `"auto"` = sceglie l’interfaccia UP con più traffico totale
  - oppure nome esatto interfaccia (come in `Get-NetAdapter`)

**Modalità multi (override):**
- `net_ifaces` (array o null)  
  - `null` = usa `net_iface`
  - `["auto"]` = usa *tutte* le NIC UP
  - `["Ethernet","Wi-Fi"]` = lista esplicita
- `net_mode` (string)  
  - `"aggregate"` = somma traffico di tutte le NIC elencate
  - `"separate"` = una riga per NIC

### LibreHardwareMonitor (LHM)
- `lhm_exe` (string)  
  Nome file dell’eseguibile, default `LibreHardwareMonitor.exe`
- `lhm_run_hidden` (bool)  
  Se true prova a non mostrare la finestra (CREATE_NO_WINDOW)
- `lhm_wmi_namespace` (string)  
  Default `root\\LibreHardwareMonitor` (notare escape `\\` in JSON)

### Overlay
- `overlay_enabled` (bool)  
  ON/OFF overlay all’avvio
- `overlay_format` (string)  
  Template multilinea. Token disponibili:
  - `{cpu}`  (es. `CPU 20% | 55°C`)
  - `{ram}`  (es. `RAM 8.1 GB/15.7 GB`)
  - `{net}`  (es. `NET ↓1.2 MB/s ↑120.0 KB/s`)
  - `{disk}` (es. `DISK R 2.1 MB/s W 900.0 KB/s`)
  - `{gpu}`  (es. `GPU | 12% | 48°C`)
- `overlay_pos` (string)  
  `top_left | top_right | bottom_left | bottom_right | center`
- `overlay_x`, `overlay_y` (int o null)  
  Se entrambi sono int, usati come coordinate assolute (override di `overlay_pos`)
- `overlay_locked` (bool)  
  Se true non puoi trascinarlo; se false drag libero (salvataggio coordinate al rilascio).
- `overlay_padding` (int)  
  Margine dai bordi per le posizioni a griglia
- `overlay_bg` (string)  
  Colore background (nome o es. `#RRGGBB`)
- `overlay_fg` (string)  
  Colore testo (nome o es. `#RRGGBB`)
- `overlay_font` (array)  
  Esempio: `["Segoe UI", 10]`

### Esempio completo

```jsonc
{
  "refresh_s": 1.0,
  "tooltip_lines": 6,

  "show_cpu": true,
  "show_ram": true,
  "show_disk": true,

  "show_net": true,
  "net_ifaces": ["Ethernet","Wi-Fi"],
  "net_mode": "aggregate",

  "show_temps": true,
  "show_cpu_temp": true,
  "show_gpu": true,
  "show_gpu_load": true,
  "show_gpu_temp": true,

  "overlay_enabled": true,
  "overlay_format": "{cpu}\n{ram}\n{net}\n{disk}\n{gpu}",
  "overlay_pos": "bottom_right",
  "overlay_locked": true,
  "overlay_bg": "black",
  "overlay_fg": "#00FF66",
  "overlay_font": ["Segoe UI", 10]
}
```

---

## LibreHardwareMonitor: come usarlo

1. Scarica LibreHardwareMonitor (release) e copia **`LibreHardwareMonitor.exe`** nella stessa cartella di `TrayMon.py`.
2. Avvia TrayMon.
3. Se WMI + LHM sono ok, vedrai temperature CPU e dati GPU (se abilitati in config).

Se LHM non è presente o WMI non funziona:
- l’app continua a funzionare, ma le temperature/GPU saranno `n/a`.

---

## Build Windows: EXE portabile con PyInstaller

### 1) Install PyInstaller
```powershell
pip install pyinstaller
```

### 2) Build (onefile)
```powershell
pyinstaller --onefile --noconsole --name TrayMon TrayMon.py
```

Output:
```text
dist/
└─ TrayMon.exe
```

### 3) Distribuzione consigliata
Onefile va bene, però ricorda che `config.json` deve stare accanto all’EXE per essere modificabile.

Cartella finale consigliata:

```text
TrayMon-Portable/
├─ TrayMon.exe
├─ config.json
└─ LibreHardwareMonitor.exe   (opzionale, per temperature/GPU)
```

---

## Note tecniche / Troubleshooting

- Se non vedi l’icona in tray:
  - Windows 11 può nasconderla: abilita “Mostra sempre tutte le icone” oppure trascinala nell’area visibile.
- Se la rete mostra `n/a` su VM:
  - imposta `net_ifaces: ["auto"]` oppure usa il nome esatto dell’interfaccia dal comando:
    ```powershell
    Get-NetAdapter | Select Name, Status
    ```
- Se LHM non parte:
  - controlla che `LibreHardwareMonitor.exe` sia presente nella cartella e che non sia bloccato da SmartScreen.

---

## License

MIT
