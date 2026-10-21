# Подготовка приложений в изолированном эталоне

Это отдельный этап Audit. Он не удаляет пользователя/профиль, не удаляет Appx-пакеты, не выполняет `/generalize` и не создаёт первого пользователя. Исходный рабочий диск и старый профиль остаются вне этого модуля.

## Контракт хоста

`template_prep.prepare_handoff(destination, approved_apps, nonce=None)` создаёт только новую папку и возвращает манифест. `approved_apps` содержит ровно `claude` и `firefox`. Общие поля: `path` (локальный публичный установщик), `sha256` (64 lowercase hex), `version`, `publisher` (точный subject/Publisher DN), `signer_thumbprint` (40 uppercase hex). Claude дополнительно требует `name` и версию из четырёх чисел. Firefox дополнительно требует `product_code` (GUID с фигурными скобками, uppercase), `executable` строго `Mozilla Firefox/firefox.exe`. Для Firefox согласуется точная четырёхкомпонентная PE FileVersion; предоставленный MSI имеет ProductVersion `158.0.1.0`. Пути хоста не экспортируются.

Фиксированные публичные файлы: `Run-Prepare.cmd`, `Prepare-Template.ps1`, `README.md`, `Claude.msix`, `Firefox.msi`, `handoff.json`. Поле `files` содержит SHA256 первых пяти файлов; сам манифест содержит schema=1, phase=`audit_app_provisioning`, nonce, expected_smbios_serial=`WCMASTER-<nonce>`, claude и firefox (метаданные плюс фиксированный `file`). `read_handoff(directory)` проверяет схему, перечень, пути, метаданные и хеши. Контроллер дополнительно закрепляет публичные файлы только для чтения на всё время VM.

Входы — уже утверждённые ответственным официальные установщики и их подписи. Разные MSIX identity не объединяются: официальный enterprise-пакет с Name=`Claude` не считается пакетом `AnthropicPBC.Claude` только по сходному названию и версии. Старый пакет остаётся для отдельного решения.

## Одно ручное действие

Внутри помеченной изолированной Windows запусти `Run-Prepare.cmd` и подтверди UAC. До UAC сверяются SMBIOS и SHA256. Elevated USER повторяет проверки, создаёт защищённую папку `%ProgramData%\WindowsClaudeTemplate\<nonce>` (Administrators/SYSTEM — full control, Users — read/execute), копирует публичный пакет и запускает PowerShell с `-File`.

Как в проверенном inventory bootstrap, только пять Undefined scope разрешают process-only `-ExecutionPolicy RemoteSigned`. Любая явно установленная политика сохраняет приоритет; неизвестный/неполный ответ запрещает запуск. Постоянные политики не меняются. SYSTEM, службы, session 0, reparse points и неверная VM отклоняются.

Bootstrap проверяет отсутствие незавершённой настройки Windows и pending servicing. Подтверждённый активный BitLocker запрещает Audit. Недоступная проверка BitLocker остаётся `unknown`; EFS не сканируется и остаётся `unknown`. Эти unknown не дают разрешения на будущую очистку/generalize. Они не скрываются под `clear`.

Затем регистрируется только собственный HKLM RunOnce `!WindowsClaudeTemplate-<nonce>` и вызывается системный `Sysprep.exe /audit /reboot`. После перезагрузки callback требует интерактивного локального built-in Administrator с SID ending `-500`, прежний nonce/SMBIOS, доверенную локальную папку и фазу журнала. Дополнительно требуются точный ImageState `IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT` или `IMAGE_STATE_GENERALIZE_RESEAL_TO_AUDIT` и более позднее время загрузки Windows, чем записанное перед Sysprep. По [документации Microsoft](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/windows-setup-states?view=windows-11) эти значения подтверждают маршрут Setup в Audit; сами по себе они не доказывают текущий интерактивный Audit-сеанс. Поэтому SID, nonce, журнал и смена загрузки проверяются вместе; неизвестный или неоднозначный ImageState отклоняется как UNVERIFIED. Синхронный отказ Sysprep сохраняет `audit_failed` и запрещает автоматическое продолжение.

Continuation проверяет native Authenticode `Valid`, точные subject/thumbprint/SHA256 и identity/version MSIX/MSI. Claude provisioned для всех пользователей через `Add-AppxProvisionedPackage -Online -SkipLicense -Regions all`; точный Publisher проверяется также по установленному AppxManifest.xml. Только у этого установленного файла допускается подтверждённый native tag WOF `0x80000017` для прозрачной декомпрессии; symbolic link и прочие reparse tags отклоняются, все предки остаются plain. Исходный публичный пакет никаких исключений для reparse не имеет. Firefox ставится через `msiexec /i ... /qn /norestart ALLUSERS=1`; допустимы коды 0/3010, затем проверяются product code и подписанный исполняемый файл в Program Files с точной PE FileVersion. Уже подтверждённые приложения повторно не устанавливаются.

При ошибке VM остаётся в Audit. Исправление требует просмотра диагностики; никаких автоматических удалений или принудительных выключений нет. На built-in Audit Administrator можно снова запустить тот же файл в той же подтверждённой загрузке: существующие подтверждённые фазы перепроверяются, а новый attempt хранит отдельный ограниченный журнал. Другая загрузка после подтверждённого входа требует отдельного разбора и автоматически не принимается. Не подтверждённый переход в Audit не объявляется выполненным.

## Диагностика и квитанция

Каждый attempt хранит локальные `template-output.log` (до 64 KiB, оба потока дренируются) и `template-exit.json`. Родительское окно показывает этап/код и сохранённую ошибку. Сырые installer/CIM/registry exceptions в квитанцию не попадают.

`receipt.json`: ровно schema=1, nonce, phase (`audit_requested`, `apps_verified`, `failed`), verdict (`pending`, `review_required`, `failed`), code, cleanup_allowed=false, generalize_allowed=false, gates (`pending_setup`, `pending_servicing`, `bitlocker`, `efs`: clear/active/unknown), apps (`claude`, `firefox`: boolean). Только apps_verified с обеими apps=true и code=0 подтверждает установку. Это не smoke test приложения, не готовность Sysprep и не разрешение удаления профиля.

`validate_receipt(data, expected_nonce)` и `read_receipt(path, expected_nonce)` проверяют этот контракт. После Audit финальная квитанция передаётся в COM2: ASCII `WCMASTER <nonce> <UTF8_length>\n`, затем ровно JSON UTF-8 без BOM, максимум 65536 байт, 115200 8N1. Локальная копия остаётся при отказе COM2. До `/audit /reboot` квитанция audit_requested сохраняется только локально. Контроллер отвечает за собственную интерпретацию результата и штатную остановку гостя.

Нужна нативная приёмка цепочки UAC, RunOnce/Audit, офлайн-цепочек сертификатов, установки обоих приложений и smoke test перед любым решением о старом пользователе. На хосте эти действия тестами не выполняются.
