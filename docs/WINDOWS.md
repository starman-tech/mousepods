# Windows (experimental)

Reads the AirPods head sensors on Windows through the native Bluetooth stack, so audio keeps working.
Windows gives programs no L2CAP socket, so a small kernel driver opens the AirPods channel and the
Python side reads it.

```
AirPods ── L2CAP 0x1001, MTU 2582 ── windows/aap-poc driver ── aap_windows.py ── controller (unchanged)
```

## Status

| Part | State |
|---|---|
| Driver, `windows/aap-poc` | Proof of concept, test signed. See its [README](../windows/aap-poc/README.md). |
| Python transport, `aap_windows.py` | Works on real hardware (see *Measured* below). Reconnects after the AirPods go into the case and come back. |
| Rate probe, `aap_rate_probe.py` | Runs on Linux and Windows with the same code, so the two compare line for line. |
| Controller | `--diag` and `--send` work. Mouse mode is not available: the controller falls back to `--diag`. |
| Web app | Does not start on Windows yet (`os.getuid`). The controller logs a warning and carries on. |
| Other headsets (`hid_headtracker.py`) | Not available on Windows (`fcntl`). Same: a warning, nothing else. |

### Measured

Windows 11 Pro 26200 on a physical machine, built-in Intel Bluetooth, AirPods Pro 2 (product id `0x2014`),
one earbud in the ear, Python 3.13, `python aap_rate_probe.py --sweep 20000,10000,5000,2500 --seconds 15`:

| Interval asked | No audio | Music playing | Gaps (no audio / music) |
|---|---|---|---|
| 20000 µs | 49.9 Hz | 49.8 Hz | 0 / 0 |
| 10000 µs | 99.4 Hz | 99.8 Hz | 1 / 0 |
| 5000 µs | 179.5 Hz | 169.0 Hz | 41 / 83 |
| 2500 µs | 170.4 Hz | 167.6 Hz | 69 / 91 |

Samples were never out of order. At 5000 µs the result varies from run to run: a later 8 s run gave
197.6 Hz with 2 gaps. AirPods 3 have not been tried on Windows.

## What you need

- A **test machine** with Windows 11 x64. The driver is test signed: Windows must run in test-signing
  mode, which needs Secure Boot switched off in the firmware. Do not use a machine with BitLocker you
  cannot unlock, a work-managed PC, or one with anti-cheat games.
- AirPods 3 or AirPods Pro 2, paired with that machine.
- Python 3.11 or newer (3.13 is best: finer clocks). No Python package is needed for the tests below.
  Do not `pip install -r requirements.txt` on Windows: `evdev` is Linux only.
- To build the driver: Visual Studio 2022 with *Desktop development with C++*, the Spectre-mitigated
  libraries, the Windows 11 SDK 10.0.26100, the component `Component.Microsoft.Windows.DriverKit`, and
  `nuget.exe`.

## Step 1: build the driver

In a *Developer PowerShell for VS 2022*:

```powershell
cd windows\aap-poc
nuget restore packages.config -PackagesDirectory packages
$bin = "$PWD\packages\Microsoft.Windows.WDK.x64.10.0.26100.6584\c\bin\10.0.26100.0"
$env:Path = "$bin\x64;$bin\x86;$env:Path"
$opts = '/p:Configuration=Release','/p:Platform=x64',"/p:SolutionDir=$PWD\",
        '/p:SkipPackageVerification=true','/p:ApiValidator_Enable=false'
foreach ($p in 'common\lib\bthecho.vcxproj','bthcli\sys\BthEchoSampleCli.vcxproj','bthcli\app\BthEcho.vcxproj') {
  msbuild $p @opts
}
```

Expected: `bthcli\sys\x64\Release\BthEchoSampleCli\BthEchoSampleCli.inf` and
`bthcli\app\x64\Release\BthEcho.exe` exist.

## Step 2: install the driver

Switch Secure Boot off in the firmware first: `bcdedit` refuses test signing while it is on.
Then, in an elevated PowerShell, once:

```powershell
bcdedit /set testsigning on
Import-Certificate bthcli\sys\x64\Release\BthEchoSampleCli.cer -CertStoreLocation Cert:\LocalMachine\Root
Import-Certificate bthcli\sys\x64\Release\BthEchoSampleCli.cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher
```

Reboot. "Test Mode" appears in the corner of the desktop. Pair the AirPods in Windows Settings, then,
elevated again:

```powershell
pnputil /add-driver bthcli\sys\x64\Release\BthEchoSampleCli\BthEchoSampleCli.inf /install
```

Check: `Get-PnpDevice | Where-Object InstanceId -match '74ec2172'` shows *MousePods AAP PoC (AirPods)*
with status OK.

## Step 3: try it

Put one AirPod in your ear. Only one program can hold the channel: close one before starting the next.

```powershell
python -m unittest tests.test_aap_windows           # 15 tests, no hardware; must say OK
windows\aap-poc\bthcli\app\x64\Release\BthEcho.exe 10   # the driver alone: a last line with non-zero WX/s
python aap_rate_probe.py                             # Python transport: a rate line near 100 Hz, out of order 0
python aap_rate_probe.py --sweep 20000,10000,5000,2500 --seconds 15
python airpods_mouse_controller.py --diag            # live head rates; Ctrl+C stops it
```

**AirPods 3 only.** The last line of the probe lists the RemoteHID devices announced. It must contain
`devmotion6`. If it does not, Windows negotiated a receive MTU below 2582 and the driver needs a change.

**Linux baseline.** On a Linux machine, with the same AirPods:
`python3 aap_rate_probe.py --sweep 20000,10000,5000,2500 --seconds 15`.

## If something fails

| Symptom | Likely cause |
|---|---|
| `no AirPods interface` | Driver not installed, AirPods not paired, or not connected. Device Manager should show the driver's device under the AirPods. |
| `cannot open the AAP channel (Windows error 5 or 32)` | Another program holds the channel (`BthEcho.exe`, a second controller, MagicPods). |
| `cannot open the AAP channel`, other error (121 is common) | AirPods out of range or in the closed case. Open the case, put one in the ear, retry. |
| Connected, then `no sample in 30 s` | Earbud not in the ear, or AirPods 3 without `devmotion6` (see above). |
| `out of order` above 0 | The read queue in `aap_windows.py` needs `PENDING_READS = 1`. Please report it. |
| Head moves the wrong way | The profile is not calibrated, and calibration needs the web app, which does not run on Windows yet. |

## Remove it

```powershell
pnputil /enum-drivers            # find the oemNN.inf published for BthEchoSampleCli
pnputil /delete-driver oemNN.inf /uninstall
bcdedit /set testsigning off     # then reboot and switch Secure Boot back on
```

## Not done yet

- Signing for normal users (Microsoft attestation signing needs an EV certificate).
- Mouse mode on Windows (`SendInput` in place of `uinput`).
- The web app, and with it calibration, on Windows.
- After a reconnect, the first stream request can go unanswered: motion then resumes when the controller
  asks again (up to 20 s when the last known position was the case).
- `--send` on Windows uses UDP port 8736 and cannot tell whether the controller is running.
