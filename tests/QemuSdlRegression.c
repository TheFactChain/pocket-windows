/* The Python runner inserts real QEMU functions at QEMU_FUNCTIONS. */
#define SDL_MAIN_HANDLED
#include <SDL.h>
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <math.h>

#ifndef SDL_HINT_MOUSE_RELATIVE_SCALING
#define SDL_HINT_MOUSE_RELATIVE_SCALING "SDL_MOUSE_RELATIVE_SCALING"
#endif
#define DISPLAY_GL_MODE_ES 2

struct surface { int width, height; };
struct options { int gl; };
struct sdl2_console {
    struct surface *surface;
    struct { void *con; } dcl;
    SDL_Window *real_window;
    SDL_Renderer *real_renderer;
    SDL_GLContext winctx;
    struct options *opts;
    int opengl, hidden, idx;
    int windowed_display, windowed_surface_w, windowed_surface_h;
};
static struct sdl2_console console;
static int gui_grab, gui_fullscreen, absolute_enabled, absolute_mode = 1;
static int failures, sent, mouse_x, mouse_y, mouse_dx, mouse_dy;
static int surface_width(struct surface *s) { return s->width; }
static int surface_height(struct surface *s) { return s->height; }
static struct sdl2_console *get_scon_from_window(uint32_t id)
{ return SDL_GetWindowID(console.real_window) == id ? &console : NULL; }
static bool qemu_console_is_graphic(void *con) { (void)con; return true; }
static bool qemu_input_is_absolute(void *con) { (void)con; return absolute_mode; }
static void sdl_grab_start(struct sdl2_console *s) { (void)s; gui_grab = 1; }
static void sdl_grab_end(struct sdl2_console *s) { (void)s; gui_grab = 0; }
static void sdl_update_caption(struct sdl2_console *s) { (void)s; }
static void sdl_send_mouse_event(struct sdl2_console *s, int dx, int dy,
                                int x, int y, int buttons)
{
    (void)s; (void)buttons;
    ++sent; mouse_x = x; mouse_y = y; mouse_dx = dx; mouse_dy = dy;
}

/* QEMU_FUNCTIONS */

#define CHECK(condition, ...) do { if (!(condition)) { \
    fprintf(stderr, "FAIL: "); fprintf(stderr, __VA_ARGS__); \
    fprintf(stderr, "\n"); ++failures; } } while (0)
#define SDL_OK(condition) do { if (!(condition)) { \
    fprintf(stderr, "SDL setup failed: %s\n", SDL_GetError()); exit(2); \
    } } while (0)

static void check_fitted_size(void)
{
    const int cases[][6] = {
        {1920, 1080, 2500, 1300, 1920, 1080},
        {1920, 1080, 1740, 980, 1728, 972},
        {1024, 768, 1740, 980, 1024, 768},
        {1920, 1080, 890, 490, 864, 486},
        {1920, 1200, 1740, 980, 1568, 980},
        {1366, 768, 1334, 673, 1197, 673},
    };
    unsigned int index;
    for (index = 0; index < sizeof(cases) / sizeof(cases[0]); ++index) {
        int width, height;
        const int *item = cases[index];
        sdl2_window_client_size(item[0], item[1], item[2], item[3], &width, &height);
        CHECK(width == item[4] && height == item[5],
              "surface %dx%d in %dx%d must fit as %dx%d, got %dx%d",
              item[0], item[1], item[2], item[3], item[4], item[5], width, height);
        CHECK(llabs((int64_t)width * item[1] - (int64_t)height * item[0]) <= SDL_max(item[0], item[1]),
              "surface proportions may differ by at most one physical pixel");
    }
}

/* Pushed physical events pass through SDL's real logical-size event watcher. */
static SDL_Event dispatch(Uint32 type, int x, int y, int dx, int dy)
{
    SDL_Event input, event, logical;
    int received = 0;
    SDL_FlushEvents(SDL_FIRSTEVENT, SDL_LASTEVENT);
    SDL_zero(input);
    input.type = type;
    if (type == SDL_MOUSEMOTION) {
        input.motion.windowID = SDL_GetWindowID(console.real_window);
        input.motion.x = x; input.motion.y = y;
        input.motion.xrel = dx; input.motion.yrel = dy;
    } else {
        input.button.windowID = SDL_GetWindowID(console.real_window);
        input.button.button = SDL_BUTTON_LEFT;
        input.button.state = type == SDL_MOUSEBUTTONDOWN ? SDL_PRESSED : SDL_RELEASED;
        input.button.x = x; input.button.y = y;
    }
    sent = 0;
    SDL_zero(logical);
    SDL_OK(SDL_PushEvent(&input) == 1);
    while (SDL_PollEvent(&event)) {
        if (event.type != type) { continue; }
        logical = event; ++received;
        if (type == SDL_MOUSEMOTION) { handle_mousemotion(&event); }
        else { handle_mousebutton(&event); }
    }
    CHECK(received == 1, "exactly one queued event must reach the handler (got %d)", received);
    CHECK(sent == 1, "handler must send exactly once (got %d)", sent);
    return logical;
}

static void check_mapping(int width, int height)
{
    SDL_Event event;
    float scale_x, scale_y;
    int hint, edge;
    int points[4][2] = {{0, height / 2}, {width - 1, height / 2},
                        {width / 2, 0}, {width / 2, height - 1}};
    SDL_SetWindowSize(console.real_window, width, height);
    SDL_OK(SDL_RenderSetLogicalSize(console.real_renderer, 1920, 1080) == 0);
    SDL_RenderGetScale(console.real_renderer, &scale_x, &scale_y);
    CHECK(scale_x > 0 && scale_y > 0, "renderer scale must be positive");
    SDL_SetHint(SDL_HINT_MOUSE_RELATIVE_SCALING, "1");
    gui_grab = 1; absolute_mode = 1;
    event = dispatch(SDL_MOUSEMOTION, width / 3, height / 3, 12, 8);
    CHECK(mouse_x == event.motion.x && mouse_y == event.motion.y,
          "%dx%d motion mapped twice: got %d,%d, SDL logical %d,%d",
          width, height, mouse_x, mouse_y, event.motion.x, event.motion.y);
    event = dispatch(SDL_MOUSEBUTTONDOWN, width / 3, height / 3, 0, 0);
    CHECK(mouse_x == event.button.x && mouse_y == event.button.y,
          "%dx%d button mapped twice: got %d,%d, SDL logical %d,%d",
          width, height, mouse_x, mouse_y, event.button.x, event.button.y);
    event = dispatch(SDL_MOUSEBUTTONUP, 0, height / 2, 0, 0);
    CHECK(mouse_x == event.button.x && mouse_y == event.button.y,
          "%dx%d letterbox button must preserve SDL logical coordinates", width, height);

    for (hint = 0; hint <= 1; ++hint) {
        int expected_dx, expected_dy;
        SDL_SetHint(SDL_HINT_MOUSE_RELATIVE_SCALING, hint ? "1" : "0");
        absolute_mode = 0; gui_grab = 1;
        event = dispatch(SDL_MOUSEMOTION, width / 2, height / 2, 12, 8);
        expected_dx = hint ? event.motion.xrel : (int)(event.motion.xrel / scale_x);
        expected_dy = hint ? event.motion.yrel : (int)(event.motion.yrel / scale_y);
        CHECK(mouse_dx == expected_dx && mouse_dy == expected_dy,
              "%dx%d relative hint=%d must scale once: got %d,%d expected %d,%d",
              width, height, hint, mouse_dx, mouse_dy, expected_dx, expected_dy);
    }

    absolute_mode = 1;
    for (edge = 0; edge < 4; ++edge) {
        gui_grab = 1; gui_fullscreen = 0;
        dispatch(SDL_MOUSEMOTION, points[edge][0], points[edge][1], 0, 0);
        CHECK(!gui_grab, "%dx%d edge %d must release capture", width, height, edge);
        gui_grab = 1; gui_fullscreen = 1;
        dispatch(SDL_MOUSEMOTION, points[edge][0], points[edge][1], 0, 0);
        CHECK(gui_grab, "%dx%d fullscreen edge %d must retain capture", width, height, edge);
    }
    gui_fullscreen = 0;
}

static void check_gl_legacy(void)
{
    SDL_Event event;
    int hint;
    /* Exercise the raw GL branch without creating a GL context or touching input. */
    console.opengl = 1; absolute_mode = 1; gui_grab = 1;
    SDL_SetWindowSize(console.real_window, 960, 540);
    for (hint = 0; hint <= 1; ++hint) {
        SDL_SetHint(SDL_HINT_MOUSE_RELATIVE_SCALING, hint ? "1" : "0");
        SDL_zero(event); event.type = SDL_MOUSEMOTION;
        event.motion.windowID = SDL_GetWindowID(console.real_window);
        event.motion.x = 200; event.motion.y = 150;
        event.motion.xrel = 12; event.motion.yrel = 8;
        sent = 0; handle_mousemotion(&event);
        CHECK(sent == 1 && mouse_x == 400 && mouse_y == 300 && mouse_dx == 24 && mouse_dy == 16,
              "GL raw motion must retain legacy scaling, hint=%d", hint);
    }
    SDL_zero(event); event.type = SDL_MOUSEBUTTONDOWN;
    event.button.windowID = SDL_GetWindowID(console.real_window);
    event.button.button = SDL_BUTTON_LEFT; event.button.x = 200; event.button.y = 150;
    sent = 0; handle_mousebutton(&event);
    CHECK(sent == 1 && mouse_x == 400 && mouse_y == 300, "GL raw button must retain legacy scaling");
    console.opengl = 0;
}

int main(void)
{
    struct surface surface = {1920, 1080};
    struct options options = {0};
    SDL_SetMainReady();
    SDL_OK(SDL_setenv("SDL_VIDEODRIVER", "dummy", 1) == 0);
    SDL_OK(SDL_setenv("SDL_RENDER_DRIVER", "software", 1) == 0);
    SDL_OK(SDL_Init(SDL_INIT_VIDEO) == 0);
    console.surface = &surface; console.opts = &options; console.hidden = 1;
    check_fitted_size();
    sdl2_window_create(&console);
    SDL_OK(console.real_window && console.real_renderer);
    CHECK(!(SDL_GetWindowFlags(console.real_window) & SDL_WINDOW_RESIZABLE),
          "QEMU window must not have SDL_WINDOW_RESIZABLE");
    CHECK(SDL_GetWindowFlags(console.real_window) & SDL_WINDOW_HIDDEN,
          "regression window must stay hidden");
    check_mapping(1920, 1080);
    check_mapping(960, 540);
    check_mapping(890, 490);
    check_gl_legacy();
    SDL_DestroyRenderer(console.real_renderer);
    SDL_DestroyWindow(console.real_window);
    SDL_Quit();
    if (failures) { fprintf(stderr, "%d SDL backend checks failed\n", failures); return 1; }
    puts("PASS: real QEMU SDL fixed window, single coordinate mapping, four edges, fullscreen, relative hints and legacy GL");
    return 0;
}
