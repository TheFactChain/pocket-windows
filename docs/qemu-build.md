# Сборка движка QEMU с исправлением SDL

Исходник: [QEMU 11.1.0](https://download.qemu.org/qemu-11.1.0.tar.xz),
SHA256 `6ee1d1a61f68212476b27108c26da5f449dc09b626d42f8279ba0dc2e08fa858`.
Изменения в `ui/sdl2.c` и структуре `include/ui/sdl2.h`, полный diff находится в
[`patches/qemu-11.1.0-sdl-window.patch`](../patches/qemu-11.1.0-sdl-window.patch).
Firmware, `qemu-img.exe` и DLL берутся из существующего комплекта.

Сборка выполняется в отдельном каталоге с ASCII-путём, например
`E:\CodexBuild\wcp-zos-20261023`. Нужен MSYS2 MINGW64 с MSVCRT, совместимый
с поставляемыми DLL. UCRT64 для этой сборки не используется. Проверенный набор:
GCC 16.2.0, SDL 2.32.10, libslirp 4.9.3, pixman 0.46.4, libepoxy 1.5.10,
GLib 2.90.1; в runtime остаётся GLib 2.88.3. После сборки обязательно проверяются
импортируемые имена DLL и символы по фактическому runtime, затем запуск EXE.
Также нужны make, diffutils, pkgconf, Meson, Ninja, Python, pip, setuptools и wheel.
Зависимости устанавливаются только в отдельный MSYS2, без изменения системного PATH.

В MINGW64, из каталога рядом с распакованным `qemu-11.1.0`:

```sh
git -C qemu-11.1.0 apply --check /path/to/patches/qemu-11.1.0-sdl-window.patch
git -C qemu-11.1.0 apply /path/to/patches/qemu-11.1.0-sdl-window.patch
mkdir qemu-build
cd qemu-build
../qemu-11.1.0/configure --target-list=x86_64-softmmu \
  --enable-whpx --enable-sdl --enable-slirp --enable-pixman --enable-vvfat \
  --enable-opengl --disable-gtk --disable-curses --disable-spice --disable-vnc \
  --disable-docs --disable-debug-info --disable-werror --disable-download \
  --disable-fdt --disable-tools --disable-guest-agent --disable-rust \
  --with-pkgversion=WindowsClaude-sdlfix2
ninja -j4 qemu-system-x86_64.exe
strip --strip-unneeded qemu-system-x86_64.exe
```

Исходный архив содержит абсолютную Unix-ссылку
`roms/edk2/EmulatorPkg/Unix/Host/X11IncludeHack`; при безопасной распаковке под
Windows она пропущена. Эти исходники firmware в данной сборке не используются.

`tests/test_qemu_sdl_backend.py` извлекает оконную функцию и обработчики мыши
непосредственно из подготовленного `ui/sdl2.c`, компилирует их с небольшими
заглушками QEMU и запускает с **поставляемой** SDL2.dll. Используется скрытое
окно и dummy video driver: проверка не трогает курсор и запущенные VM.
Проверяются 1920×1080, 960×540, полосы, движение и кнопки, четыре края,
сохранение полноэкранного захвата и оба режима относительных координат SDL.
Дополнительно проверяется fit по фактической поверхности, включая 4:3, 16:10 и
1366×768. Для малых сокращённых пропорций (например, 16:9) используются точные
целые кратные; для крупных единиц применяется обычный единый scale с погрешностью
округления до одного физического пикселя.

Геометрия рассчитывается внутри SDL при создании окна, смене поверхности/монитора,
восстановлении и выходе из полного экрана. Внешний контроллер размеры не меняет.
Это исключает конфликт фактического guest surface с запрошенным разрешением и
рассогласование Win32-размера с внутренним размером SDL.

Из корня исходников, в PowerShell:

```powershell
$env:WCP_QEMU_BUILD_ROOT = 'E:\CodexBuild\wcp-zos-20261023'
& '.\runtime\python-3.12.14\python.exe' -I -S -B '.\tests\test_qemu_sdl_backend.py' -v
Remove-Item Env:\WCP_QEMU_BUILD_ROOT
```

Перед установкой нового EXE проверь его с DLL именно из поставляемой runtime-папки,
закрепи SHA256 в четырёх `ENGINE_SHA256` и примени `patch_qemu_engine.py --engine`.
Затем проверь манифест, архив восстановления и восстановление движка в отдельной
тестовой копии. Нативный регрессионный тест не заменяет проверку настоящего окна
QEMU с гостевой Windows, двумя мониторами, сворачиванием и полным экраном.
