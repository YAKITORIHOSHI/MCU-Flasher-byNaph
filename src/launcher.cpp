#include <string>
#include <windows.h>

int WINAPI WinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance,
                   LPSTR lpCmdLine, int nCmdShow) {
  // 1. Get current executable's full path
  wchar_t exePath[MAX_PATH];
  if (GetModuleFileNameW(NULL, exePath, MAX_PATH) == 0) {
    MessageBoxW(NULL, L"Failed to retrieve executable path.", L"Error",
                MB_ICONERROR | MB_OK);
    return 1;
  }

  // 2. Extract parent directory
  std::wstring exeStr(exePath);
  size_t lastBackslash = exeStr.find_last_of(L'\\');
  if (lastBackslash == std::wstring::npos) {
    MessageBoxW(NULL, L"Failed to parse directory path.", L"Error",
                MB_ICONERROR | MB_OK);
    return 1;
  }
  std::wstring dirPath = exeStr.substr(0, lastBackslash);

  // 3. Construct target VBS script path
  std::wstring vbsPath = dirPath + L"\\runThisOnWindows.vbs";

  // 4. Verify the VBS file exists
  DWORD attrib = GetFileAttributesW(vbsPath.c_str());
  if (attrib == INVALID_FILE_ATTRIBUTES ||
      (attrib & FILE_ATTRIBUTE_DIRECTORY)) {
    MessageBoxW(NULL,
                L"Launcher script 'runThisOnWindows.vbs' was not found in the "
                L"application directory.",
                L"Error", MB_ICONERROR | MB_OK);
    return 1;
  }

  // 5. Build command line argument for wscript.exe: wscript.exe
  // "C:\path\to\runThisOnWindows.vbs" We quote the VBS path to handle spaces
  // correctly.
  std::wstring argStr = L"\"" + vbsPath + L"\"";

  // 6. Launch wscript.exe
  HINSTANCE result =
      ShellExecuteW(NULL, L"open", L"wscript.exe", argStr.c_str(),
                    dirPath.c_str(), SW_SHOWNORMAL);

  // ShellExecute returns value > 32 on success
  if ((INT_PTR)result <= 32) {
    MessageBoxW(NULL, L"Failed to run launcher script (runThisOnWindows.vbs).",
                L"Error", MB_ICONERROR | MB_OK);
    return 1;
  }

  return 0;
}
