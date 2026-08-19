#include <string>
#include <vector>
#include <windows.h>

static bool FileExists(const std::wstring &path) {
  DWORD attrib = GetFileAttributesW(path.c_str());
  return (attrib != INVALID_FILE_ATTRIBUTES && !(attrib & FILE_ATTRIBUTE_DIRECTORY));
}

static bool DirExists(const std::wstring &path) {
  DWORD attrib = GetFileAttributesW(path.c_str());
  return (attrib != INVALID_FILE_ATTRIBUTES && (attrib & FILE_ATTRIBUTE_DIRECTORY));
}

int WINAPI WinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance,
                   LPSTR lpCmdLine, int nCmdShow) {
  // 1. Get current executable's full path dynamically
  wchar_t exePath[32768];
  if (GetModuleFileNameW(NULL, exePath, 32768) == 0) {
    MessageBoxW(NULL, L"Failed to retrieve executable path.", L"MCU Flasher Launcher",
                MB_ICONERROR | MB_OK);
    return 1;
  }

  // 2. Extract parent directory
  std::wstring exeStr(exePath);
  size_t lastBackslash = exeStr.find_last_of(L'\\');
  if (lastBackslash == std::wstring::npos) {
    MessageBoxW(NULL, L"Failed to parse directory path.", L"MCU Flasher Launcher",
                MB_ICONERROR | MB_OK);
    return 1;
  }
  std::wstring dirPath = exeStr.substr(0, lastBackslash);

  // 3. Check candidate locations dynamically
  std::vector<std::wstring> candidates = {
      dirPath + L"\\direct\\runThisOnWindows.vbs",
      dirPath + L"\\runThisOnWindows.vbs",
      dirPath + L"\\..\\direct\\runThisOnWindows.vbs",
      dirPath + L"\\..\\runThisOnWindows.vbs"
  };

  std::wstring vbsPath = L"";
  for (const auto &candidate : candidates) {
    if (FileExists(candidate)) {
      wchar_t fullBuffer[32768];
      if (GetFullPathNameW(candidate.c_str(), 32768, fullBuffer, NULL) != 0) {
        vbsPath = fullBuffer;
      } else {
        vbsPath = candidate;
      }
      break;
    }
  }

  // 4. Verify that target VBS was located
  if (vbsPath.empty() || !FileExists(vbsPath)) {
    std::wstring errMsg =
        L"Could not find 'runThisOnWindows.vbs' in:\n" + dirPath + L"\n\n"
        L"Checked locations:\n"
        L" • " + dirPath + L"\\direct\\runThisOnWindows.vbs\n"
        L" • " + dirPath + L"\\runThisOnWindows.vbs\n\n"
        L"Please ensure MCU_Flasher.exe is located inside the MCU Flasher application folder.";
    MessageBoxW(NULL, errMsg.c_str(), L"MCU Flasher Launcher", MB_ICONERROR | MB_OK);
    return 1;
  }

  // 5. Determine working directory (application root)
  std::wstring workDir = dirPath;
  size_t vbsLastSlash = vbsPath.find_last_of(L'\\');
  if (vbsLastSlash != std::wstring::npos) {
    std::wstring vbsDir = vbsPath.substr(0, vbsLastSlash);
    if (DirExists(vbsDir + L"\\src\\modules")) {
      workDir = vbsDir;
    } else {
      size_t parentSlash = vbsDir.find_last_of(L'\\');
      if (parentSlash != std::wstring::npos) {
        std::wstring parentDir = vbsDir.substr(0, parentSlash);
        if (DirExists(parentDir + L"\\src\\modules")) {
          workDir = parentDir;
        }
      }
    }
  }

  // 6. Locate system wscript.exe dynamically
  wchar_t sysDir[MAX_PATH];
  std::wstring wscriptExe = L"wscript.exe";
  if (GetSystemDirectoryW(sysDir, MAX_PATH) > 0) {
    std::wstring testPath = std::wstring(sysDir) + L"\\wscript.exe";
    if (FileExists(testPath)) {
      wscriptExe = testPath;
    }
  }

  // 7. Launch wscript.exe with quoted VBS path to handle spaces
  std::wstring argStr = L"\"" + vbsPath + L"\"";

  HINSTANCE result =
      ShellExecuteW(NULL, L"open", wscriptExe.c_str(), argStr.c_str(),
                    workDir.c_str(), SW_HIDE);

  // ShellExecute returns value > 32 on success
  if ((INT_PTR)result <= 32) {
    std::wstring errMsg = L"Failed to run launcher script:\n" + vbsPath;
    MessageBoxW(NULL, errMsg.c_str(), L"MCU Flasher Launch Error", MB_ICONERROR | MB_OK);
    return 1;
  }

  return 0;
}
