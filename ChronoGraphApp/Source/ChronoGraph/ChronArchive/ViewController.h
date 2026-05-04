#import <UIKit/UIKit.h>

@interface ViewController : UIViewController <UIWebViewDelegate, UINavigationControllerDelegate, UIImagePickerControllerDelegate>
/* Designated initialiser.
   url     — page to load immediately (may be file:// or http/https)
   rootURL — root page for this tab; used to decide when the nav bar is hidden */
- (instancetype)initWithURL:(NSURL *)url rootURL:(NSURL *)rootURL;
@end
