#import "AppDelegate.h"
#import "ViewController.h"
#import <AVFoundation/AVFoundation.h>
#import <objc/message.h>

static NSString * const CGAPNSTokenDefaultsKey       = @"cg_apns_token";
static NSString * const CGAPNSEnvironmentDefaultsKey = @"cg_apns_environment";
NSString * const CGAPNSTokenRefreshedNotification    = @"CGAPNSTokenRefreshedNotification";

static NSString *CGHexFromData(NSData *data) {
    if (!data || ![data length]) return @"";
    const unsigned char *bytes = (const unsigned char *)[data bytes];
    NSMutableString *out = [NSMutableString stringWithCapacity:[data length] * 2];
    NSUInteger i;
    for (i = 0; i < [data length]; i++) {
        [out appendFormat:@"%02x", bytes[i]];
    }
    return out;
}

@implementation AppDelegate
@synthesize window = _window;

- (void)requestNotificationPermissionsIfPossible:(UIApplication *)application {
#if __IPHONE_OS_VERSION_MAX_ALLOWED >= 80000
    if ([application respondsToSelector:@selector(registerUserNotificationSettings:)]) {
        UIUserNotificationType types = (UIUserNotificationTypeAlert |
                                        UIUserNotificationTypeBadge |
                                        UIUserNotificationTypeSound);
        UIUserNotificationSettings *settings =
            [UIUserNotificationSettings settingsForTypes:types categories:nil];
        [application registerUserNotificationSettings:settings];
        return;
    }
#endif
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    if ([application respondsToSelector:@selector(registerForRemoteNotificationTypes:)]) {
        UIRemoteNotificationType types = (UIRemoteNotificationTypeAlert |
                                          UIRemoteNotificationTypeBadge |
                                          UIRemoteNotificationTypeSound);
        [application registerForRemoteNotificationTypes:types];
    }
#pragma clang diagnostic pop
}

- (BOOL)application:(UIApplication *)application didFinishLaunchingWithOptions:(NSDictionary *)launchOptions {
    /* Audio session — set once here so every tab's video plays through the
       silent switch without needing to repeat this per-UIWebView. */
    [[AVAudioSession sharedInstance] setCategory:AVAudioSessionCategoryPlayback error:nil];
    [[AVAudioSession sharedInstance] setActive:YES error:nil];

    self.window = [[UIWindow alloc] initWithFrame:[[UIScreen mainScreen] bounds]];
    self.window.backgroundColor = [UIColor blackColor];

    NSString *wwwBase   = [[[NSBundle mainBundle] resourcePath] stringByAppendingPathComponent:@"www"];
    NSString *assetBase = [wwwBase stringByAppendingPathComponent:@"assets"];

    /* Tab definitions — avoid @[] literal and subscript syntax for clang 3.1 compat */
    NSArray *tabDefs = [NSArray arrayWithObjects:
        [NSArray arrayWithObjects:@"pages/home.html",    @"Home",    @"HomeBtn",   nil],
        [NSArray arrayWithObjects:@"pages/search.html",  @"Search",  @"SearchBtn", nil],
        [NSArray arrayWithObjects:@"pages/chat.html",    @"Chat",    @"ChatBTN",   nil],
        [NSArray arrayWithObjects:@"pages/tools.html",   @"Tools",   @"ToolsBtn",  nil],
        [NSArray arrayWithObjects:@"pages/account.html", @"Account", @"FilesBtn",  nil],
        nil];

    NSMutableArray *navControllers = [NSMutableArray array];
    for (NSArray *def in tabDefs) {
        NSString *page  = [def objectAtIndex:0];
        NSString *label = [def objectAtIndex:1];
        NSString *icon  = [def objectAtIndex:2];

        NSString *pagePath = [wwwBase stringByAppendingPathComponent:page];
        NSURL    *pageURL  = [NSURL fileURLWithPath:pagePath];

        ViewController *vc = [[ViewController alloc] initWithURL:pageURL rootURL:pageURL];

        UINavigationController *nav = [[UINavigationController alloc] initWithRootViewController:vc];
        [nav setNavigationBarHidden:YES animated:NO];
        nav.navigationBar.barStyle = UIBarStyleBlack;
        nav.navigationBar.tintColor = [UIColor colorWithRed:0.35 green:0.78 blue:0.98 alpha:1.0];

        NSString *iconPath = [[assetBase stringByAppendingPathComponent:icon]
                              stringByAppendingPathExtension:@"png"];
        UIImage *rawIcon = [UIImage imageWithContentsOfFile:iconPath];
        /* Scale image to 30×30 pt — source PNGs are 478×478 and UIKit renders
           them nearly full-size on iOS 7 without explicit scaling. */
        UIImage *iconImg = rawIcon;
        if (rawIcon) {
            CGSize tabPt = CGSizeMake(30, 30);
            UIGraphicsBeginImageContext(tabPt);
            [rawIcon drawInRect:CGRectMake(0, 0, tabPt.width, tabPt.height)];
            UIImage *scaled = UIGraphicsGetImageFromCurrentImageContext();
            UIGraphicsEndImageContext();
            /* iOS 7+: use AlwaysOriginal (value 1) so UIKit renders the image
               as-is rather than as a translucent monochrome template icon.
               Use objc_msgSend to bypass clang 3.1 SDK type-checking. */
            SEL rwSel = @selector(imageWithRenderingMode:);
            if ([scaled respondsToSelector:rwSel])
                iconImg = ((id(*)(id,SEL,NSInteger))objc_msgSend)(scaled, rwSel, 1);
            else
                iconImg = scaled;
        }
        nav.tabBarItem = [[UITabBarItem alloc] initWithTitle:label image:iconImg tag:0];
        /* iOS 5/6 path: provide both selected+unselected images explicitly so
           UIKit does not apply blue template tinting/dimming. */
        SEL legacySel = @selector(setFinishedSelectedImage:withFinishedUnselectedImage:);
        if ([nav.tabBarItem respondsToSelector:legacySel]) {
            ((void(*)(id,SEL,id,id))objc_msgSend)(nav.tabBarItem, legacySel, iconImg, iconImg);
        }
        /* iOS 7+ path: also set selectedImage to AlwaysOriginal. */
        if ([nav.tabBarItem respondsToSelector:@selector(setSelectedImage:)]) {
            ((void(*)(id,SEL,id))objc_msgSend)(nav.tabBarItem, @selector(setSelectedImage:), iconImg);
        }

        [navControllers addObject:nav];
    }

    UITabBarController *tabs = [[UITabBarController alloc] init];
    tabs.viewControllers = navControllers;
    /* Do not set tabBar.tintColor — it reintroduces template/blue highlight behavior. */
    if ([tabs.tabBar respondsToSelector:@selector(setTranslucent:)])
        ((void(*)(id,SEL,BOOL))objc_msgSend)(tabs.tabBar, @selector(setTranslucent:), NO);

    if ([self.window respondsToSelector:@selector(setRootViewController:)]) {
        self.window.rootViewController = tabs;
    } else {
        tabs.view.frame = self.window.bounds;
        [self.window addSubview:tabs.view];
    }
    [self.window makeKeyAndVisible];
    /* Request notification permission — iOS only shows the prompt once. */
    [self requestNotificationPermissionsIfPossible:application];

    /* Detect APNs environment from the embedded provisioning profile.
       If no profile is present (common in ldid-signed IPA workflows), default
       to sandbox so dev tokens are sent to the correct APNs endpoint. */
    {
        NSString *apnsEnv = @"sandbox";
        NSString *provPath = [[NSBundle mainBundle] pathForResource:@"embedded"
                                                             ofType:@"mobileprovision"];
        if (provPath) {
            NSData *provData = [NSData dataWithContentsOfFile:provPath];
            if (provData) {
                NSString *provString = [[NSString alloc] initWithData:provData
                                                             encoding:NSISOLatin1StringEncoding];
                NSRange startRange = [provString rangeOfString:@"<?xml"];
                NSRange endRange   = [provString rangeOfString:@"</plist>"];
                if (startRange.location != NSNotFound && endRange.location != NSNotFound) {
                    NSRange plistRange = NSMakeRange(
                        startRange.location,
                        endRange.location + endRange.length - startRange.location);
                    NSString *plistString = [provString substringWithRange:plistRange];
                    NSData *plistData = [plistString dataUsingEncoding:NSUTF8StringEncoding];
                    NSDictionary *plist = [NSPropertyListSerialization
                        propertyListWithData:plistData options:0 format:nil error:nil];
                    NSString *ent = [[plist objectForKey:@"Entitlements"]
                                         objectForKey:@"aps-environment"];
                    if ([ent isEqualToString:@"development"]) {
                        apnsEnv = @"sandbox";
                    } else if ([ent isEqualToString:@"production"]) {
                        apnsEnv = @"production";
                    }
                }
            }
        }
        [[NSUserDefaults standardUserDefaults] setObject:apnsEnv
                                                  forKey:CGAPNSEnvironmentDefaultsKey];
    }

    return YES;
}

- (void)applicationDidBecomeActive:(UIApplication *)application {
    SEL regSel = NSSelectorFromString(@"registerForRemoteNotifications");
    if ([application respondsToSelector:regSel]) {
        ((void(*)(id,SEL))objc_msgSend)(application, regSel);
        return;
    }
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    if ([application respondsToSelector:@selector(registerForRemoteNotificationTypes:)]) {
        UIRemoteNotificationType types = (UIRemoteNotificationTypeAlert |
                                          UIRemoteNotificationTypeBadge |
                                          UIRemoteNotificationTypeSound);
        [application registerForRemoteNotificationTypes:types];
    }
#pragma clang diagnostic pop
}

#if __IPHONE_OS_VERSION_MAX_ALLOWED >= 80000
- (void)application:(UIApplication *)application
        didRegisterUserNotificationSettings:(UIUserNotificationSettings *)notificationSettings {
    (void)application;
    if (notificationSettings.types != UIUserNotificationTypeNone) {
        SEL regSel = NSSelectorFromString(@"registerForRemoteNotifications");
        UIApplication *shared = [UIApplication sharedApplication];
        if ([shared respondsToSelector:regSel]) {
            ((void(*)(id,SEL))objc_msgSend)(shared, regSel);
        }
    }
}
#endif

- (void)application:(UIApplication *)application
        didRegisterForRemoteNotificationsWithDeviceToken:(NSData *)deviceToken {
    (void)application;
    NSString *token = CGHexFromData(deviceToken);
    if (token && [token length]) {
        [[NSUserDefaults standardUserDefaults] setObject:token
                                                  forKey:CGAPNSTokenDefaultsKey];
        [[NSUserDefaults standardUserDefaults] synchronize];
        [[NSNotificationCenter defaultCenter]
            postNotificationName:CGAPNSTokenRefreshedNotification object:nil];
    }
    NSLog(@"[CGNATIVE] APNS token bytes=%lu", (unsigned long)[deviceToken length]);
}

- (void)application:(UIApplication *)application
        didFailToRegisterForRemoteNotificationsWithError:(NSError *)error {
    (void)application;
    NSLog(@"[CGNATIVE] APNS register failed: %@", error.localizedDescription);
}

@end
