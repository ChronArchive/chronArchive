#import "AppDelegate.h"
#import "ViewController.h"
#import <AVFoundation/AVFoundation.h>

@implementation AppDelegate
@synthesize window = _window;

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
        [NSArray arrayWithObjects:@"pages/home.html",   @"Home",   @"ChronographHomeBtn",   nil],
        [NSArray arrayWithObjects:@"pages/search.html", @"Search", @"ChronographSearchBtn", nil],
        [NSArray arrayWithObjects:@"pages/files.html",  @"Files",  @"ChronographFilesBtn",  nil],
        [NSArray arrayWithObjects:@"pages/chat.html",   @"Chat",   @"ChronographChatBtn",   nil],
        [NSArray arrayWithObjects:@"pages/tools.html",  @"Tools",  @"ChronographToolsBtn",  nil],
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
            iconImg = UIGraphicsGetImageFromCurrentImageContext();
            UIGraphicsEndImageContext();
        }
        nav.tabBarItem = [[UITabBarItem alloc] initWithTitle:label image:iconImg tag:0];

        [navControllers addObject:nav];
    }

    UITabBarController *tabs = [[UITabBarController alloc] init];
    tabs.viewControllers = navControllers;
    /* barStyle not exposed on UITabBar in iOS 5 SDK; tintColor covers styling */
    tabs.tabBar.tintColor = [UIColor colorWithRed:0.35 green:0.78 blue:0.98 alpha:1.0];


    self.window.rootViewController = tabs;
    [self.window makeKeyAndVisible];
    return YES;
}

@end
