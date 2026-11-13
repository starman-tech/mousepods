/*++

Copyright (c) Microsoft Corporation.  All rights reserved.

    THIS CODE AND INFORMATION IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY
    KIND, EITHER EXPRESSED OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
    IMPLIED WARRANTIES OF MERCHANTABILITY AND/OR FITNESS FOR A PARTICULAR
    PURPOSE.

ModuleName:

    public.h

Abstract:

    Contains definitions used both by driver and application


--*/

#pragma once

//
// Service GUID and name for the service published by our bth server device
//

/* c07508f2-b970-43ca-b5dd-cc4f2391bef4 */
DEFINE_GUID(BTHECHOSAMPLE_SVC_GUID, 0xc07508f2, 0xb970, 0x43ca, 0xb5, 0xdd, 0xcc, 0x4f, 0x23, 0x91, 0xbe, 0xf4);
extern __declspec(selectany) const PWSTR BthEchoSampleSvcName = L"BthEchoSampleSrv";

//
// Device interface exposed by our bth client device
//

/* fc71b33d-d528-4763-a86c-78777c7bcd7b */
/* MousePods AAP PoC interface: 6d0f5a3e-2b1c-4f7e-9a41-0c3e8b7d5a21 */
DEFINE_GUID(BTHECHOSAMPLE_DEVICE_INTERFACE, 0x6d0f5a3e, 0x2b1c, 0x4f7e, 0x9a, 0x41, 0x0c, 0x3e, 0x8b, 0x7d, 0x5a, 0x21);


