; MEMLOOP.COM, the RMM spec guest (prereqs/memwrite_src/mkcom.py writes the same 29 bytes by hand),
; assembled here so its map file is a real Watcom one. The reference mock models this program:
; `inc word [04F0]` at CS:010F, `jmp short` back at CS:0113, CS published at 0000:04F2.
.model tiny
.code
org 100h
public start, publish, memloop, memloop_jmp, banner
start:
    mov ah, 9
    mov dx, offset banner
    int 21h
    db 31h, 0C0h            ; xor ax, ax in mkcom.py's encoding (wasm picks 33 C0)
    mov ds, ax
publish:
    mov word ptr ds:[04F2h], cs
memloop:
    inc word ptr ds:[04F0h]
memloop_jmp:
    jmp short memloop
banner db 'RMMLOOP$'
end start
