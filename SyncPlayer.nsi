; SyncPlayer.nsi - native Windows installer (NSIS 3.x, built with makensis)
;
; Replaces the tkinter wizard that installer.py used to build through PyInstaller.
; It reproduces the same contract, so every existing caller keeps working:
;
;   SyncPlayer-Setup.exe                                  GUI wizard
;   SyncPlayer-Setup.exe /S                                NSIS silent switch
;   SyncPlayer-Setup.exe --silent                          the same, our spelling
;   SyncPlayer-Setup.exe --silent --install-dir "D:\Apps"  destination
;   SyncPlayer-Setup.exe /S /D=D:\Apps                     NSIS-native destination
;   ... --no-mpv --no-ytdlp --no-ffmpeg --no-updater
;   ... --no-desktop-shortcut --no-startmenu-shortcut --no-shortcuts
;   ... --startmenu-folder "Name" --no-launch --launch
;   env: SYNCPLAYER_SILENT, SYNCPLAYER_INSTALL_DIR, SYNCPLAYER_NO_SHORTCUTS
;
; Per-user throughout: HKCU, %LOCALAPPDATA%\Programs\SyncPlayer, no UAC prompt.
; Writes the same install.json, and the same Add/Remove entry - whose uninstall
; command is the app itself (SyncPlayer.exe --uninstall, or --uninstall --silent
; for the quiet form), exactly as the previous installer registered it.
;
; Build: makensis /V2 SyncPlayer.nsi
; Needs bundle\SyncPlayer.exe and bundle\mpv\* ; bundle\SyncPlayer-Updater.exe,
; bundle\mpv\yt-dlp.exe and bundle\mpv\ffmpeg.exe are optional payloads and are
; simply skipped when absent.
;
; Order matters here: NSIS resolves names as it compiles, so the sections come
; before .onInit (which switches sections off), and RunApp comes after the
; variables it reads.

Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"
!include "FileFunc.nsh"
!include "StrFunc.nsh"

; NSIS has no core string-locate, so StrFunc provides StrLoc; declaring it is
; what makes the ${StrLoc} form resolve.
${Using:StrFunc} StrLoc

; --- identity ---------------------------------------------------------------
!define APP_NAME "SyncPlayer"
!define APP_VERSION "1.6.13"
!define APP_VERQUAD "1.6.13.0"
!define PUBLISHER "SyncPlayer"
!define MPV_VERSION "0.41.0"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"

Name "${APP_NAME}"
OutFile "dist\SyncPlayer-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
; An existing install becomes the default, so Setup updates that copy in place.
InstallDirRegKey HKCU "${UNINST_KEY}" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64
ShowInstDetails show

VIProductVersion "${APP_VERQUAD}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "LegalCopyright" "${PUBLISHER}"

; --- state ------------------------------------------------------------------
Var Params       ; everything after the executable ($CMDLINE is the whole line)
Var ArgVal       ; a value pulled out of the command line
Var Ch           ; one character, for quote stripping
Var Len          ; string length, for quote stripping
Var LaunchAfter  ; "1" unless --no-launch
Var WantMpv
Var WantYtdlp
Var WantFfmpeg
Var WantUpdater
Var WantDesktop
Var WantStartMenu
Var StartMenuFolder
Var TimeStamp    ; "YYYY-MM-DDTHH:MM:SS", as the previous installer wrote it
Var EscDir       ; install dir with backslashes doubled, for JSON
Var EscMenu      ; Start Menu folder, likewise
Var TmpEsc       ; scratch for EscapeJson, which must not touch $R5-$R7

; --- wizard -----------------------------------------------------------------
!define MUI_ABORTWARNING
!define MUI_ICON "icon.ico"
!define MUI_WELCOMEPAGE_TITLE "${APP_NAME} ${APP_VERSION} Setup"
!define MUI_WELCOMEPAGE_TEXT "This will install ${APP_NAME} on your computer.$\r$\n$\r$\n${APP_NAME} plays two videos at once and keeps them locked together, for watching a movie alongside a reaction, a commentary or an alternate cut.$\r$\n$\r$\nIt installs for the current user only, so no administrator password is needed. Click Next to continue."
!define MUI_DIRECTORYPAGE_TEXT_TOP "Setup will install ${APP_NAME} in the folder below. To install somewhere else, click Browse. An existing installation is updated in place."
!define MUI_COMPONENTSPAGE_TEXT_TOP "Choose which parts to install. ${APP_NAME} itself is required; the rest can be left out and added later by running Setup again."
!define MUI_STARTMENUPAGE_TEXT_TOP "Setup will create the shortcuts below in the Start Menu folder named here."
!define MUI_FINISHPAGE_TITLE "${APP_NAME} is installed"
!define MUI_FINISHPAGE_TEXT "${APP_NAME} is ready.$\r$\n$\r$\nSource A is your movie and Source B is the reaction: press Start, then Play, then drag a bar until the two moments line up.$\r$\n$\r$\nYour settings, remembered alignments and screenshots are stored separately and are never touched by an update."
!define MUI_FINISHPAGE_RUN_TEXT "Run ${APP_NAME}"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION "RunApp"
!define MUI_STARTMENUPAGE_DEFAULTFOLDER "${APP_NAME}"
!define MUI_STARTMENUPAGE_REGISTRY_ROOT "HKCU"
!define MUI_STARTMENUPAGE_REGISTRY_KEY "Software\${APP_NAME}"
!define MUI_STARTMENUPAGE_REGISTRY_VALUENAME "StartMenuFolder"

!insertmacro MUI_PAGE_WELCOME
!define MUI_DIRECTORYPAGE_VERIFYONLEAVE
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE CheckDir
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_STARTMENU Application $StartMenuFolder
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

; --- sections ---------------------------------------------------------------
; Only the sections the caller leaves selected are installed, which is also what
; makes the command line switches work: they untick sections before this runs.
Section "${APP_NAME}" SecApp
  SectionIn RO
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  ; a running copy cannot be replaced
  DetailPrint "Closing a running ${APP_NAME}..."
  nsExec::ExecToLog 'taskkill /F /IM ${APP_NAME}.exe'
  Pop $0
  DetailPrint "Installing ${APP_NAME}..."
  File "bundle\${APP_NAME}.exe"
SectionEnd

Section "mpv player (recommended)" SecMpv
  SetOutPath "$INSTDIR\mpv"
  ; yt-dlp and ffmpeg sit in this folder too but have their own sections, so they
  ; are excluded here and installed by those sections instead.
  File /r /x yt-dlp.exe /x ffmpeg.exe "bundle\mpv\*.*"
SectionEnd

Section "yt-dlp (YouTube and other URL sources)" SecYtdlp
  SetOutPath "$INSTDIR\mpv"
  !if /FileExists "bundle\mpv\yt-dlp.exe"
    File "bundle\mpv\yt-dlp.exe"
  !endif
SectionEnd

Section "ffmpeg (full-quality downloads)" SecFfmpeg
  SetOutPath "$INSTDIR\mpv"
  !if /FileExists "bundle\mpv\ffmpeg.exe"
    File "bundle\mpv\ffmpeg.exe"
  !endif
SectionEnd

Section "Update checker" SecUpdater
  SetOutPath "$INSTDIR"
  !if /FileExists "bundle\${APP_NAME}-Updater.exe"
    File "bundle\${APP_NAME}-Updater.exe"
  !endif
SectionEnd

Section "Desktop shortcut" SecDesktop
  SetShellVarContext current
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_NAME}.exe" "" "$INSTDIR\${APP_NAME}.exe" 0
SectionEnd

Section "Start Menu shortcuts" SecStartMenu
  SetShellVarContext current
  ; an empty folder name means "don't create a Start Menu folder"
  ${If} $StartMenuFolder != ""
    !insertmacro MUI_STARTMENU_WRITE_BEGIN Application
      CreateDirectory "$SMPROGRAMS\$StartMenuFolder"
      CreateShortCut "$SMPROGRAMS\$StartMenuFolder\${APP_NAME}.lnk" "$INSTDIR\${APP_NAME}.exe" "" "$INSTDIR\${APP_NAME}.exe" 0
      ${If} ${FileExists} "$INSTDIR\${APP_NAME}-Updater.exe"
        CreateShortCut "$SMPROGRAMS\$StartMenuFolder\${APP_NAME} - Check for Updates.lnk" "$INSTDIR\${APP_NAME}-Updater.exe" "" "$INSTDIR\${APP_NAME}-Updater.exe" 0
      ${EndIf}
      CreateShortCut "$SMPROGRAMS\$StartMenuFolder\Uninstall ${APP_NAME}.lnk" "$INSTDIR\${APP_NAME}.exe" "--uninstall" "$INSTDIR\${APP_NAME}.exe" 0
    !insertmacro MUI_STARTMENU_WRITE_END
  ${EndIf}
SectionEnd

; Hidden and always run, and last: every selected component has been copied by
; now, so the versions recorded here describe what is really on disk.
Section "-Finish" SecFinal
  SetShellVarContext current

  ; install.json: the same keys the app and the updater read
  ; ${GetTime} slot order did not match its documentation, so ask Windows.
  ; This fills $0-$7, so it runs before the boolean strings are built.
  System::Alloc 16
  Pop $5
  System::Call "kernel32::GetLocalTime(p r5)"
  System::Call "*$5(&i2.r0,&i2.r1,&i2.r2,&i2.r3,&i2.r4,&i2.r5,&i2.r6,&i2.r7)"
  System::Free $5
  IntFmt $1 "%02u" $1   ; month
  IntFmt $3 "%02u" $3   ; day
  IntFmt $4 "%02u" $4   ; hour
  IntFmt $5 "%02u" $5   ; minute
  IntFmt $6 "%02u" $6   ; second
  StrCpy $TimeStamp "$0-$1-$3T$4:$5:$6"

  ; what was selected
  SectionGetFlags ${SecMpv} $0
  IntOp $R0 $0 & ${SF_SELECTED}
  SectionGetFlags ${SecYtdlp} $0
  IntOp $R1 $0 & ${SF_SELECTED}
  SectionGetFlags ${SecFfmpeg} $0
  IntOp $R2 $0 & ${SF_SELECTED}
  SectionGetFlags ${SecUpdater} $0
  IntOp $R3 $0 & ${SF_SELECTED}

  ; the app version comes from the executable that was just installed
  ${GetFileVersion} "$INSTDIR\${APP_NAME}.exe" $R4

  StrCpy $R5 "0"
  ${If} $R0 != 0
    StrCpy $R5 "${MPV_VERSION}"
    StrCpy $0 "true"
  ${Else}
    StrCpy $0 "false"
  ${EndIf}

  StrCpy $R6 "0"
  StrCpy $1 "false"
  ${If} $R1 != 0
    ${If} ${FileExists} "$INSTDIR\mpv\yt-dlp.exe"
      DetailPrint "Checking yt-dlp..."
      nsExec::ExecToStack /TIMEOUT=60000 '"$INSTDIR\mpv\yt-dlp.exe" --version'
      Pop $5
      Pop $R6
      ${If} $5 != "0"
        StrCpy $R6 ""
      ${EndIf}
      Call StripEol
      StrCpy $R6 $R6 40
      ${If} $R6 == ""
        StrCpy $R6 "0"
      ${Else}
        StrCpy $1 "true"
      ${EndIf}
    ${EndIf}
  ${EndIf}

  StrCpy $R7 "0"
  StrCpy $2 "false"
  ${If} $R2 != 0
    ${If} ${FileExists} "$INSTDIR\mpv\ffmpeg.exe"
      DetailPrint "Checking ffmpeg..."
      nsExec::ExecToStack /TIMEOUT=60000 '"$INSTDIR\mpv\ffmpeg.exe" -version'
      Pop $5
      Pop $R7
      ${If} $5 != "0"
        StrCpy $R7 ""
      ${EndIf}
      Call StripEol
      ; first line reads: ffmpeg version 9.0.1-essentials_build-www.gyan.dev Copyright ...
      ${StrLoc} $6 "$R7" "version " ">"
      ${If} $6 != ""
        IntOp $6 $6 + 8
        StrCpy $R7 $R7 "" $6          ; drop through "ffmpeg version "
        ${StrLoc} $6 "$R7" " " ">"
        ${If} $6 != ""
          StrCpy $R7 $R7 $6           ; up to the first space
        ${EndIf}
        ${StrLoc} $6 "$R7" "-" ">"
        ${If} $6 != ""
          StrCpy $R7 $R7 $6           ; 9.0.1-essentials_build -> 9.0.1
        ${EndIf}
      ${EndIf}
      ${If} $R7 == ""
        StrCpy $R7 "0"
      ${Else}
        StrCpy $2 "true"
      ${EndIf}
    ${EndIf}
  ${EndIf}

  StrCpy $3 "false"
  ${If} $R3 != 0
    ${If} ${FileExists} "$INSTDIR\${APP_NAME}-Updater.exe"
      StrCpy $3 "true"
    ${EndIf}
  ${EndIf}

  ; a JSON string needs its backslashes doubled, and NSIS has no string replace.
  ; $R5-$R7 hold the component versions here, so escape via $TmpEsc.
  StrCpy $TmpEsc "$INSTDIR"
  Call EscapeJson
  StrCpy $EscDir "$TmpEsc"
  StrCpy $TmpEsc "$StartMenuFolder"
  Call EscapeJson
  StrCpy $EscMenu "$TmpEsc"

  FileOpen $9 "$INSTDIR\install.json" w
  FileWrite $9 "{$\r$\n"
  FileWrite $9 '  "app_version": "$R4",$\r$\n'
  FileWrite $9 '  "mpv_version": "$R5",$\r$\n'
  FileWrite $9 '  "mpv_installed": $0,$\r$\n'
  FileWrite $9 '  "ytdlp_version": "$R6",$\r$\n'
  FileWrite $9 '  "ytdlp_installed": $1,$\r$\n'
  FileWrite $9 '  "ffmpeg_version": "$R7",$\r$\n'
  FileWrite $9 '  "ffmpeg_installed": $2,$\r$\n'
  FileWrite $9 '  "updater_installed": $3,$\r$\n'
  FileWrite $9 '  "install_dir": "$EscDir",$\r$\n'
  FileWrite $9 '  "startmenu_folder": "$EscMenu",$\r$\n'
  FileWrite $9 '  "installed_at": "$TimeStamp"$\r$\n'
  FileWrite $9 "}$\r$\n"
  FileClose $9

  ; Add/Remove Programs entry: HKCU, so no administrator rights are involved, and
  ; the uninstall command is the app itself.
  ${GetSize} "$INSTDIR" "/S=1" $6 $7 $8
  ${If} $6 == ""
    StrCpy $6 "0"
  ${EndIf}
  DetailPrint "Registering the installation..."
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "$R4"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_NAME}.exe,0"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\${APP_NAME}.exe" --uninstall'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\${APP_NAME}.exe" --uninstall --silent'
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" $6
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1

  ; a silent install never reaches the Finish page, so launch from here instead
  IfSilent 0 +3
  ${If} $LaunchAfter == "1"
    Exec "$INSTDIR\${APP_NAME}.exe"
  ${EndIf}
SectionEnd

; --- command line -----------------------------------------------------------
; Drops a single pair of surrounding double quotes from $ArgVal.
!macro Unquote
  StrCpy $Ch $ArgVal 1
  ${If} $Ch == '"'
    StrLen $Len $ArgVal
    IntOp $Len $Len - 2
    StrCpy $ArgVal $ArgVal $Len 1
  ${EndIf}
!macroend

; $ArgVal = the value of "--name value", or "" when the flag is absent.
!macro GetFlagArg FLAG
  StrCpy $ArgVal ""
  ClearErrors
  ${GetOptionsS} $Params "${FLAG}" $ArgVal
  ${IfNot} ${Errors}
    ${If} $ArgVal != ""
      !insertmacro Unquote
    ${EndIf}
  ${Else}
    StrCpy $ArgVal ""
  ${EndIf}
!macroend

; $2 = "1" when the flag is present, "0" when it is not.
!macro FlagPresent FLAG OUT
  StrCpy ${OUT} "0"
  ClearErrors
  ${GetOptionsS} $Params "${FLAG}" $2
  ${IfNot} ${Errors}
    StrCpy ${OUT} "1"
  ${EndIf}
!macroend

Function .onInit
  StrCpy $LaunchAfter "1"
  StrCpy $WantMpv "1"
  StrCpy $WantYtdlp "1"
  StrCpy $WantFfmpeg "1"
  StrCpy $WantUpdater "1"
  StrCpy $WantDesktop "1"
  StrCpy $WantStartMenu "1"
  StrCpy $StartMenuFolder "${APP_NAME}"

  ${GetParameters} $Params

  ; the environment, exactly as the previous installer honoured it
  ClearErrors
  ReadEnvStr $0 "SYNCPLAYER_INSTALL_DIR"
  ${IfNot} ${Errors}
    ${If} $0 != ""
      StrCpy $INSTDIR $0
    ${EndIf}
  ${EndIf}
  ClearErrors
  ReadEnvStr $0 "SYNCPLAYER_SILENT"
  ${IfNot} ${Errors}
    ${If} $0 != ""
      SetSilent silent
    ${EndIf}
  ${EndIf}
  ClearErrors
  ReadEnvStr $0 "SYNCPLAYER_NO_SHORTCUTS"
  ${IfNot} ${Errors}
    ${If} $0 != ""
      StrCpy $WantDesktop "0"
      StrCpy $WantStartMenu "0"
    ${EndIf}
  ${EndIf}

  ; our spelling of silent; NSIS's own /S needs no help
  !insertmacro FlagPresent "--silent" $0
  ${If} $0 == "1"
    SetSilent silent
  ${EndIf}

  !insertmacro GetFlagArg "--install-dir"
  ${If} $ArgVal != ""
    StrCpy $INSTDIR $ArgVal
  ${EndIf}
  !insertmacro GetFlagArg "--startmenu-folder"
  ${If} $ArgVal != ""
    StrCpy $StartMenuFolder $ArgVal
  ${EndIf}

  !insertmacro FlagPresent "--no-mpv" $0
  ${If} $0 == "1"
    StrCpy $WantMpv "0"
  ${EndIf}
  !insertmacro FlagPresent "--no-ytdlp" $0
  ${If} $0 == "1"
    StrCpy $WantYtdlp "0"
  ${EndIf}
  !insertmacro FlagPresent "--no-ffmpeg" $0
  ${If} $0 == "1"
    StrCpy $WantFfmpeg "0"
  ${EndIf}
  !insertmacro FlagPresent "--no-updater" $0
  ${If} $0 == "1"
    StrCpy $WantUpdater "0"
  ${EndIf}
  !insertmacro FlagPresent "--no-desktop-shortcut" $0
  ${If} $0 == "1"
    StrCpy $WantDesktop "0"
  ${EndIf}
  !insertmacro FlagPresent "--no-startmenu-shortcut" $0
  ${If} $0 == "1"
    StrCpy $WantStartMenu "0"
  ${EndIf}
  !insertmacro FlagPresent "--no-shortcuts" $0
  ${If} $0 == "1"
    StrCpy $WantDesktop "0"
    StrCpy $WantStartMenu "0"
  ${EndIf}
  !insertmacro FlagPresent "--no-launch" $0
  ${If} $0 == "1"
    StrCpy $LaunchAfter "0"
  ${EndIf}
  !insertmacro FlagPresent "--launch" $0
  ${If} $0 == "1"
    StrCpy $LaunchAfter "1"
  ${EndIf}

  ; components switched off must also be unticked on the components page
  ${If} $WantMpv == "0"
    SectionSetFlags ${SecMpv} 0
  ${EndIf}
  ${If} $WantYtdlp == "0"
    SectionSetFlags ${SecYtdlp} 0
  ${EndIf}
  ${If} $WantFfmpeg == "0"
    SectionSetFlags ${SecFfmpeg} 0
  ${EndIf}
  ${If} $WantUpdater == "0"
    SectionSetFlags ${SecUpdater} 0
  ${EndIf}
  ${If} $WantDesktop == "0"
    SectionSetFlags ${SecDesktop} 0
  ${EndIf}
  ${If} $WantStartMenu == "0"
    SectionSetFlags ${SecStartMenu} 0
  ${EndIf}
FunctionEnd

; Windows protects some folders, and failing halfway through a 150 MB copy is not
; something an installer should do.
Function CheckDir
  ${StrLoc} $0 "$INSTDIR" ":" ">"
  ${If} $0 == ""
    MessageBox MB_OK|MB_ICONEXCLAMATION "Please choose a full path, for example $LOCALAPPDATA\Programs\${APP_NAME}."
    Abort
  ${EndIf}
  StrLen $0 "$INSTDIR"
  ${If} $0 < 4
    MessageBox MB_OK|MB_ICONEXCLAMATION "Choose a folder rather than the root of a drive."
    Abort
  ${EndIf}
  ${StrLoc} $0 "$INSTDIR" "\Program Files" ">"
  ${If} $0 != ""
    MessageBox MB_OK|MB_ICONEXCLAMATION "Windows protects $INSTDIR. Choose a folder such as $LOCALAPPDATA\Programs\${APP_NAME}."
    Abort
  ${EndIf}
  ${StrLoc} $0 "$INSTDIR" "\Windows" ">"
  ${If} $0 != ""
    MessageBox MB_OK|MB_ICONEXCLAMATION "Windows protects $INSTDIR. Choose another folder."
    Abort
  ${EndIf}
FunctionEnd

; Finish page: the checkbox runs this, and --no-launch makes it do nothing.
Function RunApp
  ${If} $LaunchAfter == "1"
    Exec "$INSTDIR\${APP_NAME}.exe"
  ${EndIf}
FunctionEnd

; Trailing CR/LF (and spaces) off the string in $R6 - nsExec output ends with one.
Function StripEol
  ${Do}
    StrLen $5 $R6
    ${If} $5 == 0
      ${Break}
    ${EndIf}
    StrCpy $7 $R6 1 -1
    ${If} $7 == "$\r"
    ${OrIf} $7 == "$\n"
    ${OrIf} $7 == " "
      IntOp $5 $5 - 1
      StrCpy $R6 $R6 $5
    ${Else}
      ${Break}
    ${EndIf}
  ${Loop}
FunctionEnd

; Doubles every backslash in $R6, so it can go straight into a JSON string.
Function EscapeJson
  StrCpy $8 ""
  StrCpy $9 "0"
  ${Do}
    StrCpy $7 $TmpEsc 1 $9
    ${If} $7 == ""
      ${Break}
    ${EndIf}
    ${If} $7 == "\"
      StrCpy $8 "$8\\"
    ${Else}
      StrCpy $8 "$8$7"
    ${EndIf}
    IntOp $9 $9 + 1
  ${Loop}
  StrCpy $TmpEsc $8
FunctionEnd

; --- component descriptions (shown on the components page) ------------------
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecApp} "The player itself. Required."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMpv} "The video engine ${APP_NAME} drives. Recommended: without it, ${APP_NAME} uses an mpv already on this computer, or none at all."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecYtdlp} "Resolves YouTube and other links so they can be streamed or downloaded."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecFfmpeg} "Lets yt-dlp merge separate video and audio streams, which is how anything above roughly 720p is published. Without it, downloads are capped at single-file quality."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecUpdater} "The update check in the header of the window. Updates the player, mpv, yt-dlp and ffmpeg."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "A shortcut on the Desktop."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStartMenu} "Shortcuts in the Start Menu: the player, the update checker and an uninstall entry."
!insertmacro MUI_FUNCTION_DESCRIPTION_END
