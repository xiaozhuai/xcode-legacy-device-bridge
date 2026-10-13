#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#include <dlfcn.h>
#include <mach-o/dyld.h>
#include <string.h>

int main(int argc, char **argv) {
  @autoreleasepool {
    if (argc != 2) return 2;
    void *handle = dlopen(argv[1], RTLD_NOW | RTLD_GLOBAL);
    if (!handle) { fprintf(stderr, "dlopen failed: %s\n", dlerror()); return 1; }
    const char *classes[] = {"DVTiOSDevice", "DVTiOSDeviceLocator", "DVTiPhone", NULL};
    for (int i = 0; classes[i]; i++) {
      if (!objc_getClass(classes[i])) { fprintf(stderr, "Missing class: %s\n", classes[i]); return 1; }
    }
    const char *methods[] = {"deviceOperations", "backgroundFetchSupportStyle",
                            "supportsXPCServiceDebugging", "hasConnection", NULL};
    Class device = objc_getClass("DVTiOSDevice");
    for (int i = 0; methods[i]; i++) {
      if (!class_getInstanceMethod(device, sel_registerName(methods[i]))) {
        fprintf(stderr, "Missing device method: %s\n", methods[i]); return 1;
      }
    }
    NSString **constant = dlsym(handle, "DVTDevicePreparationErrorsOrWarningsChanged");
    if (!constant || ![*constant isEqualToString:@"DVTDevicePreparationErrorsOrWarningsChanged"]) return 1;
    BOOL sawShim = NO;
    for (uint32_t i = 0; i < _dyld_image_count(); i++) {
      const char *path = _dyld_get_image_name(i);
      if (strstr(path, "libIOS16")) { puts(path); sawShim = YES; }
    }
    if (!sawShim) return 1;
    puts("Legacy core + compatibility shims loaded successfully. This is NOT a device-debugging test.");
    return 0;
  }
}
