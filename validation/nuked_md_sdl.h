/* A stand-in for the handful of SDL2 declarations Nuked-MD's md.c and controller.c
   use, so that the referee builds and runs headless with no SDL installed.

   This file is megadrive-python's own (MIT, as the rest of this repository).  It
   declares nothing of SDL's implementation: SDL_CreateThread here simply calls the
   work function on the caller's thread, which is what "headless, one process,
   deterministic" needs.  Nothing of Nuked-MD (GPL-2.0) is copied here or anywhere
   else in this repository; see validation/build_nuked_md.sh.  */
#ifndef MEGADRIVE_PYTHON_NUKED_MD_SDL_H
#define MEGADRIVE_PYTHON_NUKED_MD_SDL_H

#include <stdint.h>
#include <stdlib.h>

#define SDLCALL

typedef uint8_t Uint8;

typedef struct SDL_Thread { int dummy; } SDL_Thread;
typedef int (*SDL_ThreadFunction)(void *);

static SDL_Thread nuked_md_fake_thread;

/* Run the work function inline.  md.c starts the emulation on this "thread" and then
   polls for events on the main one; with no window there is nothing to poll, and the
   headless driver stops the run itself (NMD_STOP), so running it inline is both
   simpler and deterministic. */
static inline SDL_Thread *SDL_CreateThread(SDL_ThreadFunction fn, const char *name,
                                           void *data)
{
    (void)name;
    fn(data);
    return &nuked_md_fake_thread;
}

static inline void SDL_WaitThread(SDL_Thread *t, int *status) { (void)t; (void)status; }
static inline void SDL_Delay(unsigned ms) { (void)ms; }
static inline void SDL_Quit(void) {}

typedef struct { int scancode; } SDL_Keysym;
typedef struct { SDL_Keysym keysym; } SDL_KeyboardEvent;
typedef struct { int type; SDL_KeyboardEvent key; } SDL_Event;

static inline int SDL_PollEvent(SDL_Event *e) { (void)e; return 0; }

enum { SDL_QUIT = 1, SDL_KEYDOWN, SDL_KEYUP };

typedef enum {
    SDL_SCANCODE_F5 = 1, SDL_SCANCODE_F7, SDL_SCANCODE_F12,
    SDL_SCANCODE_UP, SDL_SCANCODE_DOWN, SDL_SCANCODE_LEFT, SDL_SCANCODE_RIGHT,
    SDL_SCANCODE_Z, SDL_SCANCODE_X, SDL_SCANCODE_C, SDL_SCANCODE_RETURN,
    SDL_SCANCODE_A, SDL_SCANCODE_S, SDL_SCANCODE_D,
    SDL_SCANCODE_Q, SDL_SCANCODE_W, SDL_SCANCODE_E
} SDL_Scancode;

#endif
