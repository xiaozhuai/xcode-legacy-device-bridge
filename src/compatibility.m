#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#import <objc/message.h>

// Exact NSString values read from Xcode 26.6 at runtime.
// Each small shim re-exports its native Xcode 27 provider. The legacy iOS
// support core is transplanted separately; IDEFoundation stays on Xcode 27.
#if defined(IOS16_DVTFOUNDATION)
NSString *const DVTDevicePreparationErrorsOrWarningsChanged = @"DVTDevicePreparationErrorsOrWarningsChanged";
NSString *const DVTDeviceShouldIgnoreChangesDidEndNotification = @"DVTDeviceShouldIgnoreChangesDidEndNotification";
NSString *const DVTDeviceShouldIgnoreChangesWillBeginNotification = @"DVTDeviceShouldIgnoreChangesWillBeginNotification";

// The 26 C ABI takes one DVTFilePath; 27 added an extractor object argument.
// Restore the old enumeration/sentinel algorithm only for transplanted clients.
// Filter/sentinel strings and control flow verified from 26.6 disassembly.
NSArray *DVTExtractableSharedCachePathsInDirectory(id directory) {
  NSCParameterAssert(directory != nil);
  NSString *path = ((id (*)(id, SEL))objc_msgSend)(directory, sel_registerName("pathString"));
  NSFileManager *manager = [NSFileManager defaultManager];
  NSDirectoryEnumerator *enumerator = [manager enumeratorAtURL:[NSURL fileURLWithPath:path]
                                 includingPropertiesForKeys:@[NSURLIsRegularFileKey]
                                                    options:0
                                               errorHandler:^BOOL(NSURL *url, NSError *error) { return NO; }];
  NSMutableArray *result = [NSMutableArray array];
  Class pathClass = objc_getClass("DVTFilePath");
  for (NSURL *url in enumerator) {
    NSNumber *regular = nil;
    if (![url getResourceValue:&regular forKey:NSURLIsRegularFileKey error:NULL] || !regular.boolValue) continue;
    NSString *name = url.lastPathComponent;
    if ([name rangeOfString:@"."].location != NSNotFound) continue;
    if ([name rangeOfString:@"dyld_shared_cache"].location == NSNotFound) continue;
    NSString *parent = url.path.stringByDeletingLastPathComponent;
    if (![manager fileExistsAtPath:[parent stringByAppendingPathComponent:[@".copied_" stringByAppendingString:name]]]) continue;
    if ([manager fileExistsAtPath:[parent stringByAppendingPathComponent:[@".processed_" stringByAppendingString:name]]]) continue;
    id filePath = ((id (*)(id, SEL, id))objc_msgSend)((id)pathClass, sel_registerName("filePathForFileURL:"), url);
    [result addObject:filePath];
  }
  return result;
}

static char visibilityKey;
static char operationsKey;
static NSSet *legacyDeviceOperations(id self, SEL cmd) {
  // 26 copied its lock-protected operation set; 27 no longer stores it.
  @synchronized(self) {
    NSSet *operations = objc_getAssociatedObject(self, &operationsKey);
    return operations ? [[operations copy] autorelease] : [NSSet set];
  }
}
static NSUInteger visibilityPreference(id self, SEL cmd) {
  return [objc_getAssociatedObject(self, &visibilityKey) unsignedIntegerValue];
}
static void setVisibilityPreference(id self, SEL cmd, NSUInteger preference) {
  objc_setAssociatedObject(self, &visibilityKey, @(preference), OBJC_ASSOCIATION_RETAIN_NONATOMIC);
}
static BOOL legacyYes(id self, SEL cmd) { return YES; }
static BOOL legacyNo(id self, SEL cmd) { return NO; }
static BOOL shouldIgnore(id self, SEL cmd, NSUInteger preference, BOOL automaticallyVisible) {
  // Verified against 26.6 disassembly: 0=automatic, 1=visible, 2=hidden.
  return preference == 2 || (preference == 0 && !automaticallyVisible);
}
static id startOperation(id self, SEL cmd, id description, id analyticsName) {
  // Xcode 27's surviving operation API allocates the native operation object.
  id operation = ((id (*)(id, SEL))objc_msgSend)(self, sel_registerName("startUserInitiatedDeviceOperation"));
  ((void (*)(id, SEL, id))objc_msgSend)(operation, sel_registerName("setOperationDescription:"), description);
  @synchronized(self) {
    NSMutableSet *operations = objc_getAssociatedObject(self, &operationsKey);
    if (!operations) {
      operations = [NSMutableSet set];
      objc_setAssociatedObject(self, &operationsKey, operations, OBJC_ASSOCIATION_RETAIN_NONATOMIC);
    }
    [operations addObject:operation];
  }
  return operation;
}
static id startOperationOnProxy(id self, SEL cmd, id description, id analyticsName, id proxiedDevice) {
  return startOperation(self, cmd, description, analyticsName);
}
static id startUndescribedOperation(id self, SEL cmd) {
  return startOperation(self, cmd, nil, nil);
}
static void endOperationWithError(id self, SEL cmd, id operation, id error) {
  // Native 27 endDeviceOperation: has no analytics side effects.
  ((void (*)(id, SEL, id))objc_msgSend)(self, sel_registerName("endDeviceOperation:"), operation);
  @synchronized(self) {
    [objc_getAssociatedObject(self, &operationsKey) removeObject:operation];
  }
}
static NSNotificationCenter *devicePreparationCenter(id self, SEL cmd) {
  static dispatch_once_t once;
  static NSNotificationCenter *center;
  dispatch_once(&once, ^{ center = [[NSNotificationCenter alloc] init]; });
  return center;
}
static BOOL legacyHasConnection(id self, SEL cmd) {
  BOOL wired = ((BOOL (*)(id, SEL))objc_msgSend)(self, sel_registerName("hasWiredConnection"));
  return wired || ((BOOL (*)(id, SEL))objc_msgSend)(self, sel_registerName("hasWirelessConnection"));
}
static NSSet *connectionKeyPaths(id self, SEL cmd) {
  return [NSSet setWithObjects:@"hasWiredConnection", @"hasWirelessConnection", nil];
}
static NSString *remotePairingVersionScan(id self, SEL cmd, NSString *text) {
  if (![text length]) return nil;
  NSRegularExpression *regex = [NSRegularExpression regularExpressionWithPattern:@"(supportsRP-?)(\\d*)"
                                                                         options:0 error:NULL];
  NSTextCheckingResult *match = [regex firstMatchInString:text options:0 range:NSMakeRange(0, text.length)];
  if (!match) return nil;
  NSString *prefix = [text substringWithRange:[match rangeAtIndex:1]];
  if ([prefix isEqualToString:@"supportsRP"]) return @"1";
  NSString *version = [text substringWithRange:[match rangeAtIndex:2]];
  return version.length ? version : nil;
}
static NSString *legacyPlatformVersion(id self, SEL cmd) {
  NSString *platform = ((id (*)(id, SEL))objc_msgSend)(self, sel_registerName("platformIdentifier"));
  BOOL device = [platform isEqualToString:@"com.apple.platform.iphoneos"];
  BOOL simulator = [platform isEqualToString:@"com.apple.platform.iphonesimulator"];
  if (!device && !simulator) return nil;
  // Exact values from 26.6 iOS-platform xcplugindata, removed in 27.
  if (cmd == sel_registerName("minimumOSForBackgroundFetchEvents")) return @"7.0";
  if (cmd == sel_registerName("minimumOSForNetworkedDevelopment")) return device ? @"11.0" : nil;
  if (cmd == sel_registerName("minimumOSForLaunchActivateSuspended")) return @"11.3";
  if (cmd == sel_registerName("minimumOSForXPCServiceDebugging") ||
      cmd == sel_registerName("minimumOSForUnhostedXPCServiceDebugging")) return @"8.0";
  return nil;
}
static NSUInteger legacyBackgroundFetchStyle(id self, SEL cmd) {
  NSString *platform = ((id (*)(id, SEL))objc_msgSend)(self, sel_registerName("platformIdentifier"));
  // Verified 26.6 enum: 1=Device (simulator), 2=Debugger (physical iOS).
  if ([platform isEqualToString:@"com.apple.platform.iphoneos"]) return 2;
  if ([platform isEqualToString:@"com.apple.platform.iphonesimulator"]) return 1;
  return 0;
}
static NSUInteger legacyDeviceBackgroundFetchStyle(id self, SEL cmd) {
  id platform = ((id (*)(id, SEL))objc_msgSend)(self, sel_registerName("platform"));
  id info = ((id (*)(id, SEL))objc_msgSend)(platform, sel_registerName("dvt_extendedInfo"));
  return ((NSUInteger (*)(id, SEL))objc_msgSend)(info, sel_registerName("backgroundFetchSupportStyle"));
}
static BOOL legacyDeviceSupportsXPC(id self, SEL cmd) {
  id version = ((id (*)(id, SEL))objc_msgSend)(self, sel_registerName("operatingSystemVersion"));
  id platform = ((id (*)(id, SEL))objc_msgSend)(self, sel_registerName("platform"));
  id info = ((id (*)(id, SEL))objc_msgSend)(platform, sel_registerName("dvt_extendedInfo"));
  SEL minimum = cmd == sel_registerName("supportsXPCServiceDebugging")
                  ? sel_registerName("minimumOSForXPCServiceDebugging")
                  : sel_registerName("minimumOSForUnhostedXPCServiceDebugging");
  id threshold = ((id (*)(id, SEL))objc_msgSend)(info, minimum);
  return ((NSInteger (*)(id, SEL, id))objc_msgSend)(version, sel_registerName("dvt_versionNumberComparison:"), threshold) >= 0;
}
__attribute__((constructor)) static void restoreLegacyVisibilityMethods(void) {
  Class cls = objc_getClass("DVTDevice");
  if (!cls) return;
  // Add only absent methods. Never override a native Xcode 27 implementation.
  class_addMethod(cls, sel_registerName("visibilityPreference"), (IMP)visibilityPreference, "Q16@0:8");
  class_addMethod(cls, sel_registerName("setVisibilityPreference:"), (IMP)setVisibilityPreference, "v24@0:8Q16");
  class_addMethod(cls, sel_registerName("automaticallyVisible"), (IMP)legacyYes, "B16@0:8");
  class_addMethod(cls, sel_registerName("isSupportedHardware"), (IMP)legacyYes, "B16@0:8");
  class_addMethod(cls, sel_registerName("canUserIgnore"), (IMP)legacyNo, "B16@0:8");
  class_addMethod(cls, sel_registerName("shouldIgnoreGivenVisibilityPreference:automaticallyVisible:"),
                  (IMP)shouldIgnore, "B28@0:8Q16B24");
  class_addMethod(cls, sel_registerName("startDeviceOperation"), (IMP)startUndescribedOperation, "@16@0:8");
  class_addMethod(cls, sel_registerName("deviceOperations"), (IMP)legacyDeviceOperations, "@16@0:8");
  class_addMethod(cls, sel_registerName("startDeviceOperationWithDescription:analyticsName:"),
                  (IMP)startOperation, "@32@0:8@16@24");
  class_addMethod(cls, sel_registerName("startDeviceOperationWithDescription:analyticsName:proxiedDeviceUsed:"),
                  (IMP)startOperationOnProxy, "@40@0:8@16@24@32");
  class_addMethod(cls, sel_registerName("startUserInitiatedDeviceOperationWithDescription:analyticsName:"),
                  (IMP)startOperation, "@32@0:8@16@24");
  class_addMethod(cls, sel_registerName("endDeviceOperation:error:"),
                  (IMP)endOperationWithError, "v32@0:8@16@24");
  class_addMethod(object_getClass([NSNotificationCenter class]),
                  sel_registerName("devicePreparationCenter"), (IMP)devicePreparationCenter, "@16@0:8");
  class_addMethod(object_getClass((id)cls), sel_registerName("remotePairingVersionScan:"),
                  (IMP)remotePairingVersionScan, "@24@0:8@16");
  Class platformInfo = objc_getClass("DVTExtendedPlatformInfo");
  for (NSString *name in @[@"minimumOSForBackgroundFetchEvents", @"minimumOSForNetworkedDevelopment",
                           @"minimumOSForLaunchActivateSuspended", @"minimumOSForXPCServiceDebugging",
                           @"minimumOSForUnhostedXPCServiceDebugging"])
    class_addMethod(platformInfo, NSSelectorFromString(name), (IMP)legacyPlatformVersion, "@16@0:8");
  class_addMethod(platformInfo, sel_registerName("backgroundFetchSupportStyle"),
                  (IMP)legacyBackgroundFetchStyle, "Q16@0:8");
  // The modern base hasConnection returns NO. Restore the old wire/network
  // aggregation on the legacy subclass ONLY, leaving modern CoreDevice alone.
  Class legacy = objc_getClass("DVTiOSDevice");
  if (legacy) {
    class_addMethod(legacy, sel_registerName("backgroundFetchSupportStyle"),
                    (IMP)legacyDeviceBackgroundFetchStyle, "Q16@0:8");
    class_addMethod(legacy, sel_registerName("supportsXPCServiceDebugging"),
                    (IMP)legacyDeviceSupportsXPC, "B16@0:8");
    class_addMethod(legacy, sel_registerName("supportsUnhostedXPCServiceDebugging"),
                    (IMP)legacyDeviceSupportsXPC, "B16@0:8");
    class_addMethod(legacy, sel_registerName("hasConnection"), (IMP)legacyHasConnection, "B16@0:8");
    class_addMethod(object_getClass((id)legacy), sel_registerName("keyPathsForValuesAffectingHasConnection"),
                    (IMP)connectionKeyPaths, "@16@0:8");
  }
}
#elif defined(IOS16_IDEFOUNDATION)
NSString *const DVTDeviceErrorIsProxiedKey = @"DVTDeviceErrorIsProxied";
NSString *const IDERunOperationLaunchWithoutSuspending = @"IDERunOperationLaunchesWithoutSuspendingCausingPossibleLossOfEarlyDiagnostics";
#elif defined(IOS16_XCTEST)
NSString *const XCTestConfigurationFilePathEnvironmentKey = @"XCTestConfigurationFilePath";
#elif defined(IOS16_MACROFOUNDATION)
// 26.6 creates DVTMacroDefinitionTable and then mutates it. 27 split off a
// mutable subclass. An export alias redirects ONLY old-core class references.
@interface DVTMacroDefinitionTable : NSObject
@end
@interface DVTMutableMacroDefinitionTable : DVTMacroDefinitionTable
@end
@interface IOS16LegacyMacroDefinitionTable : DVTMutableMacroDefinitionTable
@end
@implementation IOS16LegacyMacroDefinitionTable
@end
#else
#error Select a provider for this shim.
#endif
