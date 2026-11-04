/* NG-ULTRA DDR reservations used by the application, payload and mailbox.
 *
 * Workspace 224 MiB (a large 8-bit segmentation model needs tens of MB per image; batches take
 * more), payload almost 16 MiB, mailbox 4 KiB; all inside the lowest 256 MiB
 * DDR window (the BSP's MPU maps at least 256 MiB from 0x80000000). The application
 * LOAD/BSS/heap/stacks must end before NX_DDR_WORKSPACE_BASE. External
 * transfers keep their logical W/B/X offsets and reserve padded writes; the
 * mailbox has its own cache lines. Host tools parse these defines.
 */
#ifndef NX_DDR_LAYOUT_H
#define NX_DDR_LAYOUT_H

#include <stdint.h>

#define NX_DDR_WORKSPACE_BASE UINT32_C(0x81000000)
#define NX_DDR_PAYLOAD_BASE UINT32_C(0x8f000000)
#define NX_DDR_MAILBOX_BASE UINT32_C(0x8ffff000)
#define NX_DDR_RESERVED_END UINT32_C(0x90000000)
#define NX_DDR_WORKSPACE_BYTES (NX_DDR_PAYLOAD_BASE - NX_DDR_WORKSPACE_BASE)
#define NX_DDR_PAYLOAD_CAPACITY (NX_DDR_MAILBOX_BASE - NX_DDR_PAYLOAD_BASE)

#endif
