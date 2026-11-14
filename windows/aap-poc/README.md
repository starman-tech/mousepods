# AirPods on Windows: proof of concept (issue #13)

Reads the AirPods motion stream on Windows through a small KMDF driver that opens the AAP
L2CAP channel (PSM `0x1001`). Proof of concept only: test signing, raw packets, no MousePods
integration yet.

Tested result, AirPods Pro (A3047), Windows 11 VM with a USB Bluetooth dongle passed through:

```
L2CAP channel to PSM 0x1001 open
== 993 packets, 958 SensorDataWX (956 with sensor blocks), 83.2 WX/s
```

## What is in here

| Path | What | License |
|---|---|---|
| `bthcli/sys/`, `common/` | Microsoft's `bluetooth/bthecho` client driver, modified | MS-PL, see `LICENSE-MS-PL` |
| `bthcli/app/main.cpp` | console program: handshake, stream request, packet count | Apache-2.0 |

Changes to the Microsoft sample (all marked `MousePods AAP PoC`):

- `BthEchoSampleCli.inx`: binds to `BTHENUM\{74ec2172-0bad-4d01-8f77-997b2be0722a}`, an Apple SDP
  service that Windows exposes for paired AirPods without a driver. The remote address comes from there.
- `device.c`: no SDP lookup, PSM fixed to `0x1001` (AirPods don't publish it in SDP).
- `client.c`: basic mode instead of ERTM, receive MTU 2582 (`IOS_MTU` in the controller).
- `public.h`: own device interface GUID `6d0f5a3e-2b1c-4f7e-9a41-0c3e8b7d5a21`.

`main.cpp` sends the same packets as `airpods_mouse_controller.py` (`PKT_HANDSHAKE`, `PKT_FEATURES`,
`PKT_NOTIFICATIONS`, then `stream()` for activity and devmotion6) and reads one L2CAP packet per
`ReadFile`.

## Build

Visual Studio 2022 (Community) with *Desktop development with C++*, the Spectre-mitigated libraries,
the Windows 11 SDK 10.0.26100 and the component `Component.Microsoft.Windows.DriverKit`. The WDK itself
comes from NuGet (10.0.26100.6584; 10.0.28000 needs Visual Studio 2026).

```powershell
cd windows\aap-poc
nuget restore packages.config -PackagesDirectory packages
$bin = "$PWD\packages\Microsoft.Windows.WDK.x64.10.0.26100.6584\c\bin\10.0.26100.0"
$env:Path = "$bin\x64;$bin\x86;$env:Path"          # stampinf.exe
$opts = '/p:Configuration=Release','/p:Platform=x64',"/p:SolutionDir=$PWD\",
        '/p:SkipPackageVerification=true','/p:ApiValidator_Enable=false'
foreach ($p in 'common\lib\bthecho.vcxproj','bthcli\sys\BthEchoSampleCli.vcxproj','bthcli\app\BthEcho.vcxproj') {
  msbuild $p @opts
}
```

## Run (test machine or VM only)

```powershell
# once, elevated, then reboot
bcdedit /set testsigning on
Import-Certificate bthcli\sys\x64\Release\BthEchoSampleCli.cer -CertStoreLocation Cert:\LocalMachine\Root
Import-Certificate bthcli\sys\x64\Release\BthEchoSampleCli.cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher
# pair the AirPods in Windows settings first, then
pnputil /add-driver bthcli\sys\x64\Release\BthEchoSampleCli\BthEchoSampleCli.inf /install
bthcli\app\x64\Release\BthEcho.exe 10
```

## Open points

- Signing for normal users (attestation signing).
- Check the negotiated MTU (matters for AirPods 3, which only announce their sensors with a large MTU).
- A Windows transport for `airpods_mouse_controller.py` that reads this device interface instead of a
  BlueZ socket.
